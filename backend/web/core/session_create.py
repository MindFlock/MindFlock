"""Creating a session — the core of ``POST /api/instances``, callable without HTTP.

The route is a thin wrapper around :func:`create`; a team run
(``core.team_run_driver``) creates its task sessions through the very same
code, so a run's session is validated, named, claimed under the registry lock,
red-zone-prompted, queued and started exactly as one from the New dialog.

Like the other extracted modules this calls back through the server namespace
(``srv.<name>``) for everything that lives there, which keeps
``monkeypatch.setattr(server, "_foo", ...)`` working in tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional, Tuple

from fastapi.responses import JSONResponse

from backend.web.core import git_merge as _git_merge
from backend.web.core import hidden_sessions as _hidden_sessions
from backend.web.core import person_sessions as _person_sessions
from backend.web.core import plain_repo as _plain_repo
from backend.web.core import shared_branch as _shared_branch

__all__ = [
    "create",
    "create_result",
    "free_title",
    "memory_refusal",
    "title_from_prompt",
]


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


async def create(
    payload: dict,
    *,
    peer_share: str = "",
    person: str = "",
    carry_ref: str = "",
    branched_from: str = "",
) -> JSONResponse:
    """Create a session and Start it in the background (returns 202 immediately).

    Accepted ``payload`` keys:

    * ``title`` — session name (also the ENGINE.instances key). Blank is
      named from the ``prompt`` (:func:`title_from_prompt`, numbered ``-N`` to
      stay unique), or with no prompt quick-launches an auto-numbered
      ``untitled`` / ``untitled-N``. For a provisioned
      session a title that is itself a branch path (``feature/sc-123/foo``,
      matching ``^[A-Za-z0-9._/-]+$``) is used verbatim as ``new_branch`` and the
      title becomes its last segment.
    * ``program`` — agent CLI to launch (defaults to the engine default).
    * ``provisioned`` — provisioned mode (git base-clone + per-session worktree/
      clone); requires git and either a configured ``[repository].url`` or a
      chosen ``repo_path``.
    * ``workspace_strategy`` — ``"worktree"`` (default) or ``"clone"``.
    * ``story_id`` — ticket id; seeds a default title/branch when the title is
      blank.
    * ``prompt`` — initial prompt (held in the prompt queue instead of seeded
      directly when the worktree declares a setup pass, so it survives setup,
      or when the CLI takes no prompt argument). The 202 body's
      ``prompt_delivery`` says which: ``seeded`` / ``queued`` / ``none``.
    * ``repo_path`` — a user-chosen local repo to base the session on.
    * ``in_place`` — run directly in ``repo_path`` (no worktree); forced on for a
      non-git folder. Ignored in provisioned mode.
    * ``init_repo`` — ``git init`` an empty folder first (plus an initial commit).
      Combines with ``in_place``: init the folder and then work directly in it.
    * ``launch_args`` — per-session agent flags; absent means inherit the global
      default, present (even ``[]``) means use exactly these.
    * ``extra_launch_args`` — flags ADDED to the global default (ignored when
      ``launch_args`` is present).
    * ``profile_id`` — auth profile the agent runs under; absent/blank means
      inherit the global default profile, ``"default"`` pins the CLI's own
      ambient login, anything else must name a configured profile.
    * ``profile_model`` — this session's model override of the profile's own
      model pin (e.g. an OpenRouter model id); blank keeps the pin.
    * ``plan_first`` — append the plan-first instruction to ``prompt``: the
      agent lists every file it intends to touch (a ``mindflock-plan`` block)
      and waits for Go before editing. The repo's red zones are named in the
      prompt either way.
    * ``parent`` — title of a LIVE local session this one works for (an
      orchestrator agent spawning a worker); 400 ``unknown parent session``
      otherwise, 409 when the parent is over budget.
    * ``spawned`` — boolean (strict): an agent, not a human, created this
      session. Only settable here, never afterwards; it is what lets an agent
      later delete the session.
    * ``hidden_by`` — the feature that owns this session and keeps it off the
      rail, the window numbers and the prompt-target pickers (the row's
      ``hidden_by``; core.hidden_sessions). ``"verify"`` is the only value —
      what ``run_test_plan`` passes for a checklist run; absent/``""`` is an
      ordinary session; anything else is 400. The title is never the signal.
    * ``after`` — titles this worker runs after (needs ``parent``): created
      now, its task held in its prompt queue until they are done
      (core.worker_order). ``fence`` — ``{"only", "keep_out", "reason"}``: a
      per-session fence, in place before its task starts. ``overlap`` —
      ``"wait"`` (default: a fence overlapping an unfinished sibling's runs
      after it) or ``"parallel"``. ``run_member`` — a team run's own member:
      never ordered here. The 202 says ``prompt_delivery: "held"`` and
      ``order`` when the task is held.
    * ``base_ref`` — plain worktree sessions only (400 for provisioned or
      in-place): cut the new branch from this commit-ish of ``repo_path``
      instead of its HEAD; 400 when it names no commit. ``repo_path`` stays the
      canonical repo, so cleanup never depends on where the ref came from.
    * ``base_branch`` — with ``base_ref``: the branch recorded as the session's
      diff/stage base (default: ``base_ref`` itself when it is a local branch,
      else the repo's current branch).
    * ``continue_branch`` — with ``base_ref: "<remote>/<x>"``: work ON branch
      ``x`` itself (shared with whoever pushed it) instead of cutting a new
      branch. The remote is fetched (bounded) and must have ``x``; the local
      ``x`` is created tracking it when absent (fast-forwarded when strictly
      behind). 409 naming the worktree when ``x`` is already checked out in
      one of this repo's worktrees; 400 for a non-remote ``base_ref``, a
      provisioned or in-place session. The engine records ``x`` as the
      session's branch and base exactly as for any existing branch (and never
      deletes it); the row carries ``shared_branch: true``
      (core.shared_branch).
    * ``playbook`` — ``"split"`` (the New dialog's "Split across workers"):
      decorate ``prompt`` with the split playbook (the task stays its first
      line; idempotent) so the agent splits it across worker sessions through
      its MindFlock tools. Forces a worktree (``in_place`` is ignored). 400
      for any other playbook, an empty prompt, a non-git folder, or a CLI
      that doesn't get the MindFlock tools (attach off or unsupported). The
      session records it (``Playbook``, the row's ``playbook``): it is an
      orchestrator from its first prompt, before its first worker exists.

    Spawn limits, checked under the registry lock as the title is claimed (409
    naming the knob): a ``parent`` keeps at most ``MINDFLOCK_MAX_CHILDREN``
    (8) live children and the new session's depth (root = 0) at most
    ``MINDFLOCK_MAX_SPAWN_DEPTH`` (3); a ``spawned`` session keeps the live
    total of spawned sessions at most ``MINDFLOCK_MAX_SPAWNED`` (24). The env
    knobs are read per request.

    The three creation modes are provisioned, plain-worktree, and in-place. A
    409 is returned when the title already exists; the instance registers as
    Loading and its real Start (worktree/clone + provisioning + tmux) runs in a
    background task, so a failure is surfaced via a ``session.create_failed``
    event rather than in the 202 response. ``session.created`` carries
    ``parent`` / ``spawned`` in its data when set.

    ``person`` (a keyword, never a payload key — a caller can't name someone
    else) is the person the create is for: the route passes the request's
    (:func:`backend.web.core.auth.request_person`); blank means this
    device's own. Recorded in core.person_sessions; a peer-link session
    records none (the peer is not this person).

    ``carry_ref`` and ``branched_from`` are keywords too, passed only by
    ``POST /api/instances/{title}/branch`` (core.session_branch): a snapshot
    commit of the source worktree's uncommitted changes, laid over the new
    worktree before its agent starts (needs ``base_ref``; the engine's
    ``InstanceOptions.carry_ref``), and the source's title, which
    ``session.created`` carries as ``branched_from``.
    """
    srv = _server()
    payload = payload or {}
    title = (payload.get("title", "") or "").strip()
    program = (payload.get("program", "") or "").strip() or srv.ENGINE.default_program()

    # --- Peer links -------------------------------------------------------------
    # ``peer_share`` is a keyword only the peer service passes (never a payload
    # key): it creates THAT share's sandboxed session, in place, on exactly its
    # folder. Every other create is refused a folder under the peer root —
    # checked on the RESOLVED path (symlinks, "..") before anything touches it.
    from backend.web.core import peer_guard as _peer_guard

    if peer_share:
        err = _peer_share_payload_error(payload, peer_share, program)
        if err:
            return JSONResponse({"error": err}, status_code=400)
    else:
        import os as _os

        raw_repo = str(payload.get("repo_path", "") or "")
        refused = _peer_guard.path_refusal(_os.path.expanduser(raw_repo))
        if refused is not None:
            return refused
        parent_title = str(payload.get("parent", "") or "").strip()
        if parent_title and _peer_guard.is_peer_session(
            srv.ENGINE.instances.get(parent_title)
        ):
            return JSONResponse(
                {"error": "a shared-folder (peer) session can't spawn sessions"},
                status_code=409,
            )

    # --- Optional provisioned mode --------------------------------------------
    is_provisioned = bool(payload.get("provisioned", False))
    workspace_strategy = (payload.get("workspace_strategy") or "worktree").strip()
    story_id = str(payload.get("story_id", "") or "").strip()
    prompt = payload.get("prompt", "") or ""
    repo_path = str(payload.get("repo_path", "") or "")
    new_branch = ""
    # Per-session launch flags (e.g. --dangerously-skip-permissions for just
    # this session), appended after the provider's own saved defaults every time
    # this session's agent (re)starts. The New dialog pre-fills this field with
    # the global default and always sends the key, so what arrives IS the
    # session's flags — a default the user toggled off is honored, not
    # re-applied. When the key is ABSENT (other creators / API callers), we pass
    # None so the session inherits the global default (Settings → Coding CLI).
    # Validated with the same rules as provider-level saved args so a malformed
    # payload never reaches the shell command builder.
    if "launch_args" in payload:
        try:
            launch_args = srv.provider_config.validate_launch_args(
                payload.get("launch_args") or []
            )
        except ValueError as err:
            return JSONResponse({"error": str(err)}, status_code=400)
    elif "extra_launch_args" in payload:
        # ADDITIVE flags (the MCP's spawn_session): the user's configured
        # defaults for this CLI stay, these are appended — an orchestrator
        # adding "--model x" must not strip a worker's skip-permissions.
        try:
            extra = srv.provider_config.validate_launch_args(
                payload.get("extra_launch_args") or []
            )
        except ValueError as err:
            return JSONResponse({"error": str(err)}, status_code=400)
        launch_args = srv._instance.merge_launch_args(
            srv._instance.provider_default_launch_args(program), extra
        )
    else:
        launch_args = None  # not specified -> inherit the global default

    # Auth profile pin. Rejecting an unknown id HERE beats a session that
    # launches half-authenticated and only fails when the CLI does.
    profile_id = str(payload.get("profile_id", "") or "").strip()
    err = srv._profile_id_error(profile_id)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    profile_model = str(payload.get("profile_model", "") or "").strip()
    err = srv._profile_model_error(profile_model)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    # Lineage. ``parent`` names the live session this one works for (an
    # orchestrator spawning a worker); ``spawned`` marks an agent-made session
    # and is only ever set here. Both are checked again under the registry lock
    # below, with the spawn limits, at the moment the title is claimed.
    parent = str(payload.get("parent", "") or "").strip()
    spawned = payload.get("spawned", False)
    if spawned is None:
        spawned = False
    if not isinstance(spawned, bool):
        # Strict on purpose: "spawned" unlocks agent-driven deletion, so a
        # string "false" must not read as True.
        return JSONResponse({"error": "spawned must be a boolean"}, status_code=400)
    if parent and parent not in srv.ENGINE.instances:
        return JSONResponse(
            {"error": "unknown parent session: %s" % parent}, status_code=400
        )
    # ``hidden_by``: the feature that owns this session and keeps it off the
    # rail (core.hidden_sessions). Validated here, recorded once the title is
    # claimed below. The TITLE is never the signal — ``verify-anything`` is a
    # fine name for a person's own session and it stays listed.
    hidden_by = payload.get("hidden_by") or ""
    hidden_err = _hidden_sessions.validate(hidden_by)
    if hidden_err:
        return JSONResponse({"error": hidden_err}, status_code=400)
    # Worker order + fences (see core.worker_order): checked before anything
    # is claimed, so a bad fence costs nothing.
    order_files = None
    if payload.get("fence") or payload.get("after"):
        _f, fence_problems = srv._worker_order_driver.fence_from(payload)
        if fence_problems:
            return JSONResponse({"error": "; ".join(fence_problems)}, status_code=400)
        if _f and _f.get("only") and parent:
            # The parent's tracked files: two fences covering one existing
            # file overlap even when their globs look unrelated.
            try:
                pwt = srv.ENGINE.instances[parent].GetWorktreePath()
                order_files = await asyncio.to_thread(_git_merge.tracked_files, pwt)
            except Exception:  # noqa: BLE001
                order_files = None
    if parent and await asyncio.to_thread(srv._budget_locked, parent):
        return JSONResponse(
            {
                "error": "parent session %s is over budget — raise its budget "
                "before it spawns more sessions" % parent,
                "budget_locked": True,
            },
            status_code=409,
        )
    # Memory guard: an agent fanning out workers is how a big job runs the
    # machine out of memory, so an agent's create waits for headroom. A team
    # run's own members are held by its planner instead (they stay queued
    # rather than failing here), and a person's create is never refused: the
    # memory banner already says what they are walking into.
    if spawned and not payload.get("run_member"):
        mem_refusal = await asyncio.to_thread(memory_refusal)
        if mem_refusal is not None:
            return JSONResponse(mem_refusal, status_code=409)
    # Split across workers: the agent fans the task out through its own
    # MindFlock tools, so it must GET them (attach on, a CLI that attaches)
    # and must have a task. Workers fork from its commits, so it runs in a
    # worktree of its own — never in place.
    playbook = payload.get("playbook")
    if playbook is not None and playbook != "":
        if playbook != "split":
            return JSONResponse(
                {"error": 'playbook must be "split" (got %r)' % (playbook,)},
                status_code=400,
            )
        reason = srv._mcp_unattachable(program)
        if reason is not None:
            return JSONResponse(
                {
                    "error": "Split across workers needs the MindFlock tools: %s"
                    % reason
                },
                status_code=400,
            )
        if not str(prompt).strip():
            return JSONResponse(
                {"error": "Split across workers needs a task: describe what to split"},
                status_code=400,
            )
    split = playbook == "split"
    # Fork point: cut the worktree from this commit-ish instead of the repo's
    # HEAD (a worker forking from its orchestrator's commit), recording
    # ``base_branch`` as the session's diff base. Plain worktree sessions only.
    base_ref = str(payload.get("base_ref", "") or "").strip()
    base_branch = str(payload.get("base_branch", "") or "").strip()
    continue_branch = str(payload.get("continue_branch", "") or "").strip()
    if continue_branch and is_provisioned:
        return JSONResponse(
            {
                "error": "continue_branch is only supported for plain worktree "
                "sessions (not provisioned)"
            },
            status_code=400,
        )
    if (base_ref or base_branch) and is_provisioned:
        return JSONResponse(
            {
                "error": "base_ref is only supported for plain worktree sessions "
                "(not provisioned)"
            },
            status_code=400,
        )

    if is_provisioned:
        # Provisioning is all git (base clone + worktree/clone per session).
        if not srv.git_available():
            return JSONResponse(
                {
                    "error": "provisioned mode needs git installed — install git, "
                    "or start a plain session (any folder works)"
                },
                status_code=400,
            )
        # Provisioning works for the configured [repository].url OR any local
        # repo the user picked (repo_path). Only the no-repo-chosen flow needs
        # the config to resolve.
        if not repo_path and not srv.provisioning.provisioning_available():
            return JSONResponse(
                {
                    "error": "provisioned mode needs a configured repository "
                    "(config.toml [repository].url) or a chosen local repo"
                },
                status_code=400,
            )
        if workspace_strategy not in ("worktree", "clone"):
            return JSONResponse(
                {"error": "workspace_strategy must be 'worktree' or 'clone'"},
                status_code=400,
            )
        # If the Name field is itself a full branch (e.g. a Shortcut ticket
        # branch like "feature/sc-17436/grafana-dashboard-…"), use it verbatim
        # as the branch and set the session name to just its last segment.
        if title and "/" in title and re.match(r"^[A-Za-z0-9._/-]+$", title):
            new_branch = title.strip("/")
            title = new_branch.split("/")[-1]
        else:
            # Default the title from the story id when one is given.
            if not title and story_id:
                title = "sc-%s" % story_id
            if title:
                # Deterministic branch: feature/sc-<id>/<slug> with a story,
                # else mindflock/<title>.
                new_branch = srv.provisioning.branch_name_for(story_id or None, title)

    # Whether WE invented this name. A title the caller typed is theirs and a
    # collision is theirs to hear about; one we generated is ours to make unique,
    # which is what the numbering below is for — and what the claim further down
    # has to keep doing rather than answering 409 for a request in which nobody
    # typed a name at all.
    auto_title = not title
    # An empty Name with a task is named FROM the task ("add-readme-section-
    # setup"), so the row says what it is doing; with no task either it is
    # named for the folder it works in ("sitecheck-bot7"), and only a launch
    # with neither is "untitled". Either way numbered to stay unique (titles
    # key ENGINE.instances).
    title_base = (
        "" if title else (title_from_prompt(prompt) or title_from_place(repo_path))
    )
    if not title:
        title = free_title(title_base) if title_base else srv._free_untitled()
        # Provisioned sessions derive their branch from the title; the empty
        # title skipped that above, so derive it from the generated one.
        if is_provisioned and not new_branch:
            new_branch = srv.provisioning.branch_name_for(story_id or None, title)
    if title in srv.ENGINE.instances:
        return JSONResponse(
            {"error": "instance %s already exists" % title}, status_code=409
        )
    # A renamed session goes by its new name everywhere, so that name is taken
    # too — a new session may not be called what another one is called.
    renamed = srv._aliases.owner_of(title, srv._session_branches())
    if renamed:
        return JSONResponse(
            {
                "error": "another session is already named “%s”" % title,
                "session": renamed,
            },
            status_code=409,
        )

    # --- Sessions based off a user-chosen local repo --------------------------
    # Without this, plain sessions would default to the server's own cwd (the
    # mindflock repo). The session still uses CS's isolated-worktree model — the
    # worktree is created off the picked repo's HEAD on a fresh branch. A
    # provisioned session with a chosen repo runs the SAME provisioning
    # (setup commands / cache env) against that repo (universal flow).
    plain_path = "."
    provision_repo = ""
    in_place = False
    git_enabled = True
    if repo_path or not is_provisioned:
        in_place = (
            bool(payload.get("in_place", False)) and not is_provisioned and not split
        )
        # Combinable with in_place, deliberately: "git init this folder, then work
        # directly in it" is the natural way to start a brand-new project, and
        # suppressing the init for in-place sessions silently dropped the tick and
        # handed the user a git-less session in the folder they had just asked to
        # make a repo of. _prepare_plain_repo inits + makes the initial commit, so
        # the in-place session comes up on a real branch with git features on.
        # The pair that genuinely cannot coexist is in_place and provisioning
        # (a separate worktree/clone), which the line above already enforces.
        init_repo = bool(payload.get("init_repo", False))
        # A folder that doesn't exist is made only when the request says so
        # (the New dialog's explicit "Create this folder"): a typo must not
        # become a new repo, a running agent and the next Recent folder.
        # (Passed only when set, so the two-argument call stays the default.)
        create_kw = (
            {"create_folder": True} if payload.get("create_folder") is True else {}
        )
        try:
            plain_path, git_enabled = await asyncio.to_thread(
                srv._prepare_plain_repo, repo_path, init_repo, **create_kw
            )
        except _plain_repo.NoFolderError as err:
            return JSONResponse(
                {
                    "error": str(err),
                    "detail": str(err),
                    "reason": "no_folder",
                    "path": err.path,
                },
                status_code=400,
            )
        except ValueError as err:
            return JSONResponse({"error": str(err)}, status_code=400)
        if not peer_share:
            refused = _peer_guard.path_refusal(plain_path)
            if refused is not None:
                return refused
        # A non-git folder has no HEAD to fork a worktree from and can't be
        # provisioned — run it in-place, with git features simply disabled.
        if not git_enabled:
            if is_provisioned:
                return JSONResponse(
                    {
                        "error": "provisioning needs a git repo — pick a git repo, or "
                        "tick 'Create a git repo in this folder' in Advanced"
                    },
                    status_code=400,
                )
            if split:
                return JSONResponse(
                    {
                        "error": "Split across workers needs a git repo — workers "
                        "fork from its commits; pick a git repo, or tick 'Create "
                        "a git repo in this folder' in Advanced"
                    },
                    status_code=400,
                )
            in_place = True
        if is_provisioned:
            provision_repo = plain_path
    shared_remote = ""
    if continue_branch:
        # Work ON the pushed branch: validated (and the local branch readied)
        # now, so a bad pick is a 400/409 here rather than a create_failed.
        if in_place:
            return JSONResponse(
                {
                    "error": "continue_branch is only supported for plain worktree "
                    "sessions (not in-place)"
                },
                status_code=400,
            )
        status, shared_remote, err = await asyncio.to_thread(
            _shared_branch.prepare_continue,
            plain_path,
            base_ref or "origin/" + continue_branch,
            continue_branch,
        )
        if status:
            return JSONResponse({"error": err}, status_code=status)
        # The engine's existing-branch path takes it from here: no fork point.
        base_ref = base_branch = ""
    if base_ref or base_branch:
        # An in-place session runs ON the folder's checkout — there is no new
        # branch to cut from anywhere.
        if in_place:
            return JSONResponse(
                {
                    "error": "base_ref is only supported for plain worktree sessions "
                    "(not in-place)"
                },
                status_code=400,
            )
        err = await asyncio.to_thread(
            srv._lineage.base_ref_error, plain_path, base_ref, base_branch
        )
        if err:
            return JSONResponse({"error": err}, status_code=400)
        # The engine refuses to cut a base_ref branch whose name is taken (a
        # closed or paused namesake keeps its branch) — say so NOW, as a 409,
        # rather than 202 and an asynchronous create_failed nobody reads.
        if base_ref:
            err = await asyncio.to_thread(
                srv._lineage.branch_taken_error,
                plain_path,
                srv._session_branch_name(title),
            )
            # A name WE derived from the task is ours to renumber: a closed
            # namesake's branch is no reason to refuse a request that typed
            # no name at all.
            n = 2
            while err and auto_title and title_base and n < 10:
                cand = free_title("%s-%d" % (title_base, n))
                n += 1
                err = await asyncio.to_thread(
                    srv._lineage.branch_taken_error,
                    plain_path,
                    srv._session_branch_name(cand),
                )
                if not err:
                    title = cand
            if err:
                return JSONResponse({"error": err}, status_code=409)
    # The provisioned twin: a closed session keeps its worktree (holding the
    # deterministic branch) or its clone (at a deterministic path) — say so as
    # a 409 now, not a create_failed later or a silently adopted old clone.
    if is_provisioned and new_branch:
        err = await asyncio.to_thread(
            srv.provisioning.provisioned_branch_taken_error,
            workspace_strategy,
            new_branch,
            provision_repo,
        )
        if err:
            return JSONResponse({"error": err}, status_code=409)

    # Red zones + plan-first: the launch prompt names the repo's zones and, when
    # the New Session "Plan first" box was ticked, asks for a file plan before
    # any edit. Keyed off the folder the session is cut from — its repo
    # identity is the future worktree's (same origin).
    if split:
        # First, so the task stays the prompt's first line (the pane pins it)
        # and the zone / plan-first notes follow the split instructions.
        try:
            prompt = srv._playbooks.decorate_prompt(
                "split", prompt, {"provider": srv.providers.resolve(program).name}
            )
        except srv._playbooks.PlaybookError as perr:
            return JSONResponse({"error": str(perr)}, status_code=400)
    if prompt and not peer_share:

        def _decorate(p=prompt, local=bool(repo_path or not is_provisioned)):
            dirs = [plain_path] if local else srv._repo_url_workdirs("")
            return srv._red_zone_prompt(
                p, program, dirs, bool(payload.get("plan_first"))
            )

        prompt = await asyncio.to_thread(_decorate)
    inst = srv.session.NewInstance(
        srv.session.InstanceOptions(
            title=title,
            path=plain_path,
            program=program,
            branch=continue_branch,
            provisioned=is_provisioned,
            workspace_strategy=workspace_strategy,
            provision_repo=provision_repo,
            new_branch=new_branch,
            prompt=prompt,
            launch_args=launch_args,
            in_place=in_place,
            profile_id=profile_id,
            profile_model=profile_model,
            base_ref=base_ref,
            base_branch=base_branch,
            parent=parent,
            spawned=spawned,
            playbook="split" if split else "",
            peer_share=peer_share,
            carry_ref=carry_ref if base_ref else "",
        )
    )
    # O4: every session gets a deterministic dev-server port block, injected
    # into the agent's tmux env at launch (PORT / MINDFLOCK_PORT_BASE). Not a
    # shared-folder one: nothing of the host env rides into its sandbox.
    inst.ExtraEnv = {} if peer_share else srv._ports.env_for(title)

    # O2: per-worktree setup (repo-committed .mindflock.toml [workspace]).
    # Plain worktree sessions only — provisioned workspaces run their own
    # setup, and in-place sessions share the repo dir (deps already there).
    setup_cfg = None
    if git_enabled and not is_provisioned and not in_place and not peer_share:
        setup_cfg = srv._wt_setup.load_config(plain_path)
    # Start does the heavy lifting (git worktree/clone + provisioning + tmux),
    # which can take minutes on the first worktree run (one-time base clone +
    # uv sync). Register the instance as "loading" and run Start in the
    # background so the create request returns immediately and the session shows
    # as provisioning in the grid instead of freezing the dialog on "Creating…".
    inst.SetStatus(srv.Loading)
    # THE TITLE IS CLAIMED UNDER THE LOCK, AND RE-CHECKED THERE. The 409 above
    # is a read with no claim, and everything between it and here can await —
    # `_prepare_plain_repo` alone is a whole thread hop — so two creates for one
    # title (two tabs, a stale row, the same Run pressed twice) both passed the
    # gate and the second overwrote the first's record. Nothing then owned the
    # first session: its tmux and its worktree carried on, invisible, and the
    # next attempt at that title died in Start with "tmux session already
    # exists". That is where the orphan verify sessions come from, and it is why
    # this re-check is not merely defensive.
    with srv.ENGINE.lock:
        if title in srv.ENGINE.instances:
            # A NAME WE INVENTED IS OURS TO RE-INVENT. The numbering above ran
            # before the thread hop, so two quick launches inside that window
            # both derived "untitled" and the second would have been refused —
            # a hard error for a request in which the user typed nothing at all.
            # Provisioned sessions are excluded because their BRANCH is derived
            # from the title too, and re-naming here would leave the two saying
            # different things.
            if auto_title and not is_provisioned:
                title = free_title(title_base) if title_base else srv._free_untitled()
                inst.Title = title
            else:
                return JSONResponse(
                    {"error": "instance %s already exists" % title}, status_code=409
                )
        # Lineage, re-checked where it counts: the parent may have gone while
        # the repo was prepared, and the spawn caps only hold if the count and
        # the claim happen under one lock (two concurrent spawns must not both
        # squeeze under the last slot).
        if parent and parent not in srv.ENGINE.instances:
            return JSONResponse(
                {"error": "unknown parent session: %s" % parent}, status_code=400
            )
        limit_err = srv._lineage.spawn_limit_error(
            srv.ENGINE.instances, parent, spawned
        )
        if limit_err:
            return JSONResponse({"error": limit_err}, status_code=409)
        srv.ENGINE.instances[title] = inst
    # Under the claimed title (an untitled quick-launch was renumbered above).
    # A reused title starts unhidden: delete drops the entry, and setting ""
    # here is a no-op, so an ordinary session never inherits a dead run's flag.
    _hidden_sessions.set_hidden_by(title, hidden_by)
    # Who it's for (core.person_sessions) — also resets a reused title.
    _person_sessions.set_person(title, "" if peer_share else _creator(person))
    # Working on someone's pushed branch (core.shared_branch) — also resets a
    # reused title.
    _shared_branch.set_shared(title, shared_remote, continue_branch)

    # An ordered worker (it runs after another, is fenced, or waits for a free
    # slot) is created now but its task is parked until its turn.
    order = None
    if parent or payload.get("fence"):
        # It parks the task itself (queue off + held) before registering the
        # worker, so the order loop never sees a held worker without it.
        order, order_err = srv._worker_order_driver.on_create(
            parent, title, inst, payload, order_files, prompt
        )
        if order_err:
            with srv.ENGINE.lock:
                if srv.ENGINE.instances.get(title) is inst:
                    srv.ENGINE.instances.pop(title, None)
            return JSONResponse({"error": order_err}, status_code=400)
    # How the initial prompt reaches the agent: seeded at launch (the CLI takes
    # a prompt argument, or the provisioned launcher types it in), or held in
    # the prompt queue and typed once the agent is idle. A plain session on a
    # CLI with no prompt argument (a custom script, aider, goose, …) has no
    # launch-time seed at all — without the queue its task silently vanished.
    prompt_delivery = "seeded" if prompt else "none"
    if prompt and order and order.get("held"):
        inst.Prompt = ""  # parked in its queue by on_create
        prompt_delivery = "held"
    hold_prompt = (
        bool(prompt)
        and prompt_delivery != "held"
        and (
            (setup_cfg is not None and setup_cfg.has_setup)
            or (not is_provisioned and not srv._provider_seeds_prompt(program))
            # A sandboxed launch can't read the seed file (it lives outside
            # the sandbox): a shared session's prompt is always typed in.
            or bool(peer_share)
        )
    )
    if hold_prompt:
        # Hold the initial prompt until setup succeeds: deliver it via the
        # prompt queue (drained only once setup is ok + the agent is idle)
        # instead of seeding the agent CLI directly. A failed setup keeps
        # the prompt visible in the queue rather than losing it.
        #
        # AFTER the claim above, not before: a create that loses the race must
        # not leave its prompt in the winner's queue, which is a real prompt
        # sent to a real agent by a request that answered 409.
        try:
            inst.Prompt = ""
            srv._prompt_queue.enqueue(title, prompt)
            # held=False: a namesake's stale "held" must not park this one.
            srv._prompt_queue.set_flags(title, enabled=True, held=False)
            prompt_delivery = "queued"
        except Exception as err:  # noqa: BLE001
            # A FULL OR UNWRITABLE QUEUE MUST NOT COST THE SESSION. Both calls
            # can raise (`prompt_queue._save` re-raises, and `enqueue` refuses a
            # full queue), and this is the one window where an exception is
            # unrecoverable: the title is claimed and `_bg_start` has not been
            # scheduled yet, so the session would sit in the list as Loading for
            # ever. Fall back to seeding the prompt the ordinary way — it loses
            # the "hold it until setup succeeds" guarantee, which is a smaller
            # loss than the session.
            inst.Prompt = prompt
            if srv.log.ErrorLog is not None:
                srv.log.ErrorLog.Printf(
                    "queueing the initial prompt for %s failed (%v) — seeding it "
                    "directly instead",
                    title,
                    err,
                )
    # What this session was asked to do, for its spawn record in the parent's
    # Thread — the queue path above clears inst.Prompt.
    srv._thread.note_seed(title, srv._created_epoch(inst), prompt)
    srv._mark_onboarded()  # the checklist's "Pick a repo" step: done on the first-ever session

    def _remember_folder() -> None:
        # Remember the folder this session chose so the NEXT New Session dialog
        # opens on it and the repo suggestions rank it first — the second
        # session in a repo should not be another walk down the folder tree.
        # "." is the server's own cwd (a provisioned session with no chosen
        # repo), which the user never picked, so it isn't worth remembering.
        # Only once the session has actually STARTED: a create that dies in
        # the background must not become the next Recent folder.
        if plain_path and plain_path != "." and not peer_share:
            try:
                from backend.config import settings as _settings

                _settings.update_settings(general={"last_repo_path": plain_path})
            except Exception:  # noqa: BLE001 — a convenience hint never fails a create
                pass

    async def _bg_start() -> None:
        try:
            await asyncio.to_thread(inst.Start, True)
            srv.ENGINE.save()
            await asyncio.to_thread(_remember_folder)
            if setup_cfg is not None and setup_cfg.has_setup:
                try:
                    srv._wt_setup.start_setup(
                        title, plain_path, inst.GetWorktreePath(), setup_cfg
                    )
                except Exception:  # noqa: BLE001 — setup is best-effort
                    pass
        except Exception as err:  # noqa: BLE001
            if srv.log.ErrorLog is not None:
                srv.log.ErrorLog.Printf("failed to create instance %s: %v", title, err)
            # ONLY OUR OWN RECORD, and only OUR failure to report — see
            # :func:`_drop_failed_start`. A title that now belongs to a live
            # session must not be popped (that is how an orphan is minted) and
            # must not be reported (stamping "the verify session couldn't start"
            # on a plan whose agent is working is worse than saying nothing).
            # A title nobody owns is still ours to report: the session was
            # deleted while it provisioned, and the checklist waiting on it has
            # to hear that rather than sit in ``running`` until prune.
            if not srv._drop_failed_start(title, inst):
                return
            # ...and if a CHECKLIST was waiting on this session, tell the
            # checklist. `POST /run` answered 202 long before this point and
            # stamped the plan `running`, so without this the row goes on saying
            # an agent is checking something for the thirty seconds until
            # `prune` releases it — and then reverts with the reason recorded
            # nowhere. See `test_plans.fail_run`.
            try:
                pid = srv._test_plans.find_by_run_session(title)
                if pid:
                    srv._test_plans.fail_run(pid, title, str(err))
            except Exception:  # noqa: BLE001 — never mask the create failure
                pass
            # Surface the failure to watchers (UI toast, `mindflock events`,
            # the CLI's create poll) — a session silently vanishing from the
            # list is the worst failure mode.
            srv._note_create_failure(title, str(err))
            srv._events.BUS.emit(
                "session.create_failed", session=title, data={"error": str(err)}
            )

    # Tracked in _BG_TASKS so lifespan teardown cancels it (an untracked task
    # can also be GC'd mid-flight).
    srv._register_task(_bg_start())
    # Seed the *_changed diff snapshot with the initial state so the first real
    # transition (loading->running etc.) emits instead of being swallowed (F6).
    srv._seed_event_snapshot(title)
    created_data = {
        "program": program,
        "provisioned": is_provisioned,
    }
    if parent:
        created_data["parent"] = parent
    if spawned:
        created_data["spawned"] = True
    if branched_from:
        created_data["branched_from"] = branched_from
    srv._events.BUS.emit(
        "session.created",
        session=title,
        new="loading",
        data=created_data,
    )
    body = srv._instance_json(inst)
    body["prompt_delivery"] = prompt_delivery
    if order:
        body["order"] = order
    # An account with no route for this agent runs the session on the CLI's own
    # login. The New dialog says so at selection time; API and CLI callers had
    # no way to hear it at all, and a session quietly launching as the wrong
    # identity is the one outcome this feature cannot be silent about.
    try:
        from backend.providers import auth_profiles

        note = auth_profiles.unsupported_note(program or "", profile_id)
        if note:
            body["note"] = note
    except Exception:  # noqa: BLE001 — the note is enrichment only
        pass
    return JSONResponse(body, status_code=202)


# Words that say nothing about a task, dropped from a name made from it.
_TITLE_FILLER = frozenset(
    "a an the to of in on at for and or with by from into please can could "
    "you we i my our your this that it is be some".split()
)
#: Longest name made from a task (a branch is ``<prefix><name>``).
TITLE_FROM_PROMPT_MAX = 32


def title_from_prompt(prompt: str) -> str:
    """A session name made from its task: the first non-empty line's first
    few words, lowercased and hyphenated, filler dropped — "Add a README
    section about setup" → ``add-readme-section-about-setup``. "" when the
    task has no letters or digits. The New dialog previews the same rule
    (``frontend/src/components/dialogs/newSession.ts``: titleFromPrompt)."""
    first = next((ln for ln in str(prompt or "").splitlines() if ln.strip()), "")
    words = re.findall(r"[a-z0-9]+", first.lower())
    kept = [w for w in words if w not in _TITLE_FILLER] or words
    out = ""
    for w in kept[:6]:
        nxt = out + "-" + w if out else w
        if len(nxt) > TITLE_FROM_PROMPT_MAX:
            break
        out = nxt
    if not out and kept:
        out = kept[0][:TITLE_FROM_PROMPT_MAX]
    return out


def title_from_place(repo_path: str) -> str:
    """A session name made from WHERE it works when it has no task to be named
    from: the folder's own name, lowercased, non-word runs hyphenated —
    ``/home/me/Allure/sitecheck-bot7`` → ``sitecheck-bot7``. "" for no folder
    or one with no letters or digits in its name (the caller then falls back
    to ``untitled``). The New dialog previews the same rule
    (``newSession.ts``: titleFromFolder)."""
    # Split on both separators: a Windows path from the desktop app's folder
    # picker names its folder the same way.
    name = re.split(r"[\\/]", str(repo_path or "").rstrip("/\\"))[-1]
    words = re.findall(r"[a-z0-9]+", name.lower())
    return "-".join(words)[:TITLE_FROM_PROMPT_MAX].strip("-")


def free_title(base: str) -> str:
    """``base``, or ``base-2``, ``base-3``… — the first name no session (live
    or renamed) has. :func:`server._free_untitled` for a name made from a task."""
    srv = _server()
    branches = srv._session_branches()
    title, n = base, 2
    while title in srv.ENGINE.instances or srv._aliases.owner_of(title, branches):
        title = "%s-%d" % (base, n)
        n += 1
    return title


def _creator(person: str) -> str:
    """``person``, else this device's own person id. Never raises."""
    if person:
        return person
    try:
        from backend.web.core import person as _person

        return _person.person_id()
    except Exception:  # noqa: BLE001
        return ""


def memory_refusal() -> Optional[dict]:
    """The 409 body refusing an agent-spawned create while memory is low, or
    None. It carries ``memory`` (the reading) so a caller can tell this apart
    from the other 409s and wait instead of giving up. Blocking (reads
    /proc) — call it off the event loop."""
    from backend import memory_pressure as _mem

    snap = _mem.snapshot()
    if snap.get("level") not in ("low", "critical"):
        return None
    return {
        "error": "%s — new agent-spawned sessions wait until it recovers; "
        "try again in a few minutes, or finish/close some sessions first"
        % _mem.describe(snap),
        "memory": snap,
    }


def _peer_share_payload_error(payload: dict, peer_share: str, program: str) -> str:
    """Why a shared-folder session create is malformed ("" = fine): it runs
    in place on exactly its share's folder, on a CLI with a sandbox profile,
    with no lineage, fence, playbook, fork point or provisioning."""
    import os

    from backend.peer import launch as _peer_launch
    from backend.peer import paths as _peer_paths

    try:
        work = _peer_paths.share_paths(peer_share)["work"]
    except ValueError:
        return "bad share id"
    repo = str(payload.get("repo_path", "") or "")
    if not repo or os.path.realpath(repo) != os.path.realpath(work):
        return "a shared-folder session runs in its share's folder"
    try:
        _peer_launch.provider_name(program)
    except _peer_launch.PeerLaunchError as err:
        return str(err)
    for key in (
        "parent",
        "spawned",
        "hidden_by",
        "after",
        "fence",
        "playbook",
        "base_ref",
        "base_branch",
        "continue_branch",
        "provisioned",
        "init_repo",
        "story_id",
    ):
        if payload.get(key):
            return "%s is not allowed for a shared-folder session" % key
    if not payload.get("in_place"):
        return "a shared-folder session runs in place"
    return ""


async def create_result(
    payload: dict, *, peer_share: str = "", person: str = ""
) -> Tuple[int, dict]:
    """:func:`create` as ``(status_code, body)`` for in-process callers: 202
    with the new row (plus ``prompt_delivery``), or 4xx with ``{"error"}``."""
    resp = await create(payload, peer_share=peer_share, person=person)
    try:
        body = json.loads(resp.body)
    except Exception:  # noqa: BLE001 — every branch above answers JSON
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}
