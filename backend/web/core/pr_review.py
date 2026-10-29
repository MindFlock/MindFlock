"""Force-start PR reviews from the web UI (Intake → Pull requests).

The automated monitor (``backend.ticket_ingestion.pr_monitor``) only picks
up open PRs that are (a) authored by the token's own user, (b) past the
min-age grace period and (c) absent from state.json's ``processed_prs``
ledger — so a PR can silently never get reviewed (most commonly: it was
recorded as processed back when it had no actionable comments yet, and new
comments never re-trigger it). This module backs the Intake → Pull requests
tab: it lists every non-draft open PR on the watched repos annotated with
*why* auto review is or isn't taking it, and force-starts a review session
for any of them, bypassing those filters.

The forced path reuses the pipeline's own pieces (comment fetch, PR workspace
provisioner, consolidated prompt) but runs inside the web server process, so
it works even while ingestion is stopped. ``server.py`` owns the engine side
(registering the session in the live grid).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from backend.providers import effort as _provider_effort
from backend.web.core import pipeline_root
from backend.web.core.ticket_start import LaunchError

__all__ = ["LaunchError", "launch"]

_logger = logging.getLogger(__name__)


def _resolve_repo_root() -> Path:
    """Where the pipeline keeps state.json / workspaces/ — see
    :func:`backend.web.core.pipeline_root.resolve_repo_root`."""
    return pipeline_root.resolve_repo_root(__file__)


# The pipeline runs with cwd = repo root and keeps state.json / workspaces/
# there; this server's cwd is not guaranteed to match, so every relative path
# is anchored here explicitly.
_REPO_ROOT = _resolve_repo_root()


def _load_config():
    """The pipeline's config, ``workspace_dir`` anchored at :data:`_REPO_ROOT`
    (:func:`backend.web.core.pipeline_root.load_config`)."""
    return pipeline_root.load_config(_REPO_ROOT)


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import) —
    :func:`launch` registers the session in its live engine, and calling back
    through its namespace keeps ``monkeypatch.setattr(server, …)`` working."""
    from backend.web import server

    return server


def review_agent(repo: str = "") -> str:
    """The coding CLI a PR-review session should run, ``""`` = the app default.

    Reads the SAME chain the automated pipeline does
    (:meth:`PipelineConfig.pr_agent` — the repo's own card, then Intake → Pull
    requests' screen-wide Agent CLI stored as ``github.agent``, then
    ``[mindflock].agent``). The forced-review route used to launch
    ``ENGINE.default_program()`` outright, so the Agent CLI dropdown sitting
    directly above the "Begin review" button governed only the auto monitor:
    clicking Begin review launched the app-wide default instead of the provider
    the user had picked for reviews.

    Best-effort by design: this is a launch path, and an unconfigured or
    unreadable ingestion config should fall through to the app default rather
    than block the review.
    """
    try:
        cfg = _load_config()
    except Exception:  # noqa: BLE001 — ingestion may not be configured at all
        return ""
    fn = getattr(cfg, "pr_agent", None)
    try:
        return (fn(repo) if callable(fn) else "") or ""
    except Exception:  # noqa: BLE001
        return ""


def session_title(pr) -> str:
    """Engine session title for a PR review — ``pr-<repo-name>-<number>`` (via
    :func:`pr_slug`), so #7 in two different repos never share a session, and a
    forced review collides with the auto monitor's session for the same PR."""
    from backend.ticket_ingestion.models import pr_slug

    return f"pr-{pr_slug(pr)}"


def skip_reasons(
    pr, processed: set, login: str, gh, now: datetime | None = None
) -> list[str]:
    """Why the auto monitor would skip ``pr`` right now (empty = eligible).

    Mirrors the exact filters in ``PRMonitor.scan`` so the UI explains the
    monitor's behavior instead of guessing at it.
    """
    reasons: list[str] = []
    if (pr.repo, pr.number) in processed:
        reasons.append("already handled before")
    if login and pr.author != login:
        reasons.append(f"opened by {pr.author}, not you ({login})")
    now = now or datetime.now(timezone.utc)
    age_min = (now - pr.created_at).total_seconds() / 60.0
    # This repo's own grace period, which its card may have overridden.
    min_age = gh.min_age_for(pr.repo)
    if age_min < min_age:
        left = max(1, round(min_age - age_min))
        reasons.append(f"too new — waits {left} more min")
    # The monitor asks GitHub only for PRs into this repo's base branch, so a PR
    # into another one is invisible to auto review. The panel lists every open
    # PR, which means it has to say so — otherwise a PR sitting there with no
    # chips reads as "queued" when the monitor will never see it.
    base = gh.base_branch_for(pr.repo)
    if base and pr.base_ref != base:
        reasons.append(f"into {pr.base_ref}, not {base}")
    return reasons


async def list_open_prs() -> dict:
    """Every non-draft open PR on the watched repos, annotated for the UI."""
    from backend.ticket_ingestion.pr_monitor import PRMonitor
    from backend.ticket_ingestion.state import load_processed_prs

    cfg = _load_config()
    gh = cfg.github
    if gh is None or not gh.repo_list():
        return {"repos": [], "enabled": False, "login": "", "prs": []}

    monitor = PRMonitor(gh)
    login, login_error = "", ""
    try:
        login = await monitor._authenticated_user_login()
    except Exception as err:  # noqa: BLE001 — no token / network; still list PRs
        login_error = str(err)
        _logger.warning("Could not resolve GitHub login: %s", err)

    prs = []
    for repo in gh.repo_list():
        # all_bases: the panel lists every open PR and explains the skips in
        # chips, so a base-branch filter must not silently remove rows — it
        # becomes a reason instead (and stays force-reviewable).
        prs.extend(await monitor._list_prs(repo, all_bases=True))

    from backend.ticket_ingestion.models import pr_slug

    processed = load_processed_prs(_REPO_ROOT)
    now = datetime.now(timezone.utc)
    # The dir PR workspaces are provisioned under. Optional by design: a config
    # that doesn't resolve one costs the rows their reopen probe, which is a
    # missing button — never a missing PR.
    workspace_dir = str(getattr(cfg, "workspace_dir", "") or "")
    out = []
    for pr in sorted(prs, key=lambda p: p.created_at, reverse=True):
        reasons = skip_reasons(pr, processed, login, gh, now)
        out.append(
            {
                "repo": pr.repo,
                "number": pr.number,
                "title": pr.title,
                "url": pr.url,
                "author": pr.author,
                "base_ref": pr.base_ref,
                "head_ref": pr.head_ref,
                "created_at": pr.created_at.isoformat(),
                "session": session_title(pr),
                # Where a review of this PR keeps its clone. Derived exactly as
                # ``PRProvisioner`` derives it (and pure, unlike provisioning
                # itself), so the reopen probe in server.py can tell whether an
                # earlier review's workspace is still on this machine.
                "workspace_path": (
                    str(Path(workspace_dir) / f"pr-{pr_slug(pr)}")
                    if workspace_dir
                    else ""
                ),
                "eligible": not reasons,
                "reasons": reasons,
            }
        )
    return {
        "repos": gh.repo_list(),
        "enabled": gh.enabled,
        "login": login,
        "login_error": login_error,
        "prs": out,
    }


async def find_pr(repo: str, number: int):
    """The live GitHub record for one open PR (drafts excluded)."""
    from backend.ticket_ingestion.pr_monitor import PRMonitor

    cfg = _load_config()
    if cfg.github is None:
        raise LookupError("PR review is not configured — add a repository first")
    monitor = PRMonitor(cfg.github)
    # all_bases, matching the panel: forcing a review is exactly how you take a
    # PR the auto monitor's base filter skips.
    for pr in await monitor._list_prs(repo, all_bases=True):
        if pr.number == number:
            return pr
    raise LookupError(
        f"No open PR #{number} in {repo} — closed, merged or draft PRs "
        "can't be force-reviewed"
    )


async def prepare_review(pr) -> tuple[Path, str, int]:
    """Provision the PR's workspace and build its session prompt.

    Slow (git clone/fetch) — run off the request path. Returns the workspace
    directory, the prompt, and how many actionable comments it addresses.
    """
    from backend.ticket_ingestion.pr_comments import fetch_actionable_comments
    from backend.ticket_ingestion.pr_provisioner import PRProvisioner
    from backend.ticket_ingestion.pr_runner import build_consolidated_pr_prompt

    cfg = _load_config()
    comments = await fetch_actionable_comments(pr, cfg.github)
    workspace = await PRProvisioner(cfg).provision(pr, launch_cursor=False)
    if comments:
        prompt = build_consolidated_pr_prompt(pr, comments, workspace.directory)
    else:
        prompt = _self_review_prompt(pr, workspace.directory)
    return workspace.directory, prompt, len(comments)


def _self_review_prompt(pr, workspace_dir) -> str:
    """Prompt for a forced review with no actionable reviewer comments: review
    the diff itself instead of addressing feedback."""
    return "\n".join(
        [
            f"# PR #{pr.number}: {pr.title}",
            "",
            f"- PR URL: {pr.url}",
            f"- Branch (already checked out): `{pr.head_ref}` @ `{pr.head_sha}`",
            f"- Base branch: `{pr.base_ref}`",
            f"- Workspace: `{workspace_dir}`",
            f"- Author: {pr.author}",
            "",
            "## Your task",
            "",
            "This review was started manually and the PR has no unresolved "
            "actionable reviewer comments, so review the change itself:",
            "",
            f"1. Read the full diff of this branch against `{pr.base_ref}` "
            f"(`git diff {pr.base_ref}...HEAD`).",
            "2. Look for real defects — correctness bugs, missing edge cases, "
            "broken error handling, security issues, missing test coverage.",
            "3. Fix what you find directly in this workspace. Leave the changes "
            "**unstaged** — do NOT run `git add`, `git commit`, or `git push`; "
            "the human reviews them in their editor and decides what to commit.",
            "4. Finish with a concise summary of every issue found, noting which "
            "you fixed and which need a human decision.",
        ]
    )


def record_reviewed(pr) -> None:
    """Mark ``pr`` processed so auto review doesn't run it a second time."""
    from backend.ticket_ingestion.models import ProcessedPR
    from backend.ticket_ingestion.state import (
        load_processed_prs,
        record_processed_pr,
    )

    if (pr.repo, pr.number) in load_processed_prs(_REPO_ROOT):
        return
    record_processed_pr(
        _REPO_ROOT,
        ProcessedPR(
            number=pr.number,
            head_sha=pr.head_sha,
            processed_at=datetime.now(timezone.utc),
            repo=pr.repo,
        ),
    )


async def launch(
    repo: str,
    number: int,
    *,
    program: str = "",
    depth: str = "",
    effort: str = "",
    plan: str = "",
) -> dict:
    """Start a review session for one open PR and return the 202 body.

    The body of ``POST /api/github/prs/review`` (which validates its payload
    and calls this), callable without HTTP: ``POST /api/flights/start`` with a
    ``pr`` subject calls it too. The workspace is provisioned and the session
    registered on a background task; a launch failure is reported as
    ``session.create_failed``.

    * ``program``: the CLI to run; ``""`` = this repo's review agent, then the
      app default (the route resolves the chain itself, with its own pick
      first).
    * ``depth`` / ``effort``: per-start overrides (``""`` = the repo's).
    * ``plan``: the flight plan to fly; ``""`` = ``server._intake_plan``.

    Raises :class:`LaunchError` (404 no such open PR, 409 a session for it
    already exists, 502 GitHub failed).
    """
    srv = _server()
    chosen = str(program or "")
    effort_override = str(effort or "")
    # The provisioning row goes up BEFORE the GitHub lookup below, which is the
    # last thing standing between the click and the sidebar showing anything.
    # The panel's cached list already carries the title this PR maps to, so no
    # slug re-derivation is involved; a cold cache just falls back to the
    # post-lookup registration further down.
    early = srv._cached_session_title(
        srv._OPEN_PRS_CACHE,
        "prs",
        lambda p: p.get("repo") == repo and p.get("number") == number,
    )
    if early:
        if early in srv.ENGINE.instances or srv._pending_has(early):
            raise LaunchError(
                409,
                {
                    "error": "session %s already exists — close it to re-review"
                    % early,
                    "title": early,
                },
            )
        srv._pending_add(early, "pr", repo=repo)

    try:
        pr = await find_pr(repo, number)
    except LookupError as err:
        srv._pending_drop(early)
        raise LaunchError(404, {"error": str(err)}) from None
    except Exception as err:  # noqa: BLE001
        srv._pending_drop(early)
        raise LaunchError(502, {"error": str(err)}) from None

    title = session_title(pr)
    if title != early:
        srv._pending_drop(early)  # stale cache entry — keep only the real title
        if title in srv.ENGINE.instances or srv._pending_has(title):
            raise LaunchError(
                409,
                {
                    "error": "session %s already exists — close it to re-review"
                    % title,
                    "title": title,
                },
            )
    # Re-registered with the branch now that it's known (add() keeps `since`).
    srv._pending_add(title, "pr", branch=pr.head_ref, repo=repo)
    repo_depth = srv._repo_intake_depth(repo, "prs")
    flight_tail = srv._arm_intake(
        title,
        depth or repo_depth,
        "pr",
        "%s#%s" % (repo, number),
        message=str(getattr(pr, "title", "") or ""),
        plan=srv._intake_plan(
            "pr", plan, depth, srv._repo_intake_plan(repo, "prs"), repo_depth
        ),
        subject={
            "kind": "pr",
            "source": repo,
            "id": str(number),
            "title": str(getattr(pr, "title", "") or ""),
            "url": str(getattr(pr, "url", "") or ""),
        },
        branch=str(getattr(pr, "head_ref", "") or ""),
    )

    async def _bg_review() -> None:
        # Provision first (slow: clone/fetch of the PR head), then register the
        # adopted workspace as a live session — same shape as the pipeline's
        # SessionRunner.run_pr, but against THIS server's engine so the session
        # shows up in the grid without a reload.
        try:
            directory, prompt, n_comments = await prepare_review(pr)
            # The route's pick (its own, this repo's card, Intake → Pull
            # requests' Agent CLI), else the same chain without a pick.
            # Resolved first because this start's effort has to be translated
            # into THAT CLI's spelling.
            program = chosen or review_agent(repo) or srv.ENGINE.default_program()
            prompt += flight_tail
            prompt = _provider_effort.decorate_prompt(prompt, program, effort_override)
            # The review workspace already exists here: decorate against it.
            prompt = await asyncio.to_thread(
                srv._red_zone_prompt, prompt, program, [str(directory)], None
            )
            inst = srv.session.NewInstance(
                srv.session.InstanceOptions(
                    title=title,
                    path=".",
                    program=program,
                    provisioned=True,
                    workspace_strategy="clone",
                    new_branch=pr.head_ref,
                    prompt=prompt,
                    workspace_path=str(directory),
                    launch_args=srv._start_launch_args(program, effort_override),
                )
            )
            inst.ExtraEnv = srv._ports.env_for(title)
            inst.SetStatus(srv.Loading)
            with srv.ENGINE.lock:
                srv.ENGINE.instances[title] = inst
            srv._seed_event_snapshot(title)
            srv._events.BUS.emit(
                "session.created",
                session=title,
                new="loading",
                data={"program": inst.Program, "provisioned": True, "pr": pr.number},
            )
            try:
                await asyncio.to_thread(inst.Start, True)
                srv.ENGINE.save()
            except Exception:
                # By identity: this task may be the loser of a re-start, and
                # popping by name would delete the LIVE session's record.
                srv._drop_failed_start(title, inst)
                raise
            # Ledger the PR so auto review doesn't run it a second time.
            record_reviewed(pr)
            if srv.log.InfoLog is not None:
                srv.log.InfoLog.Printf(
                    "forced PR review %s live (%d comments)", title, n_comments
                )
        except Exception as err:  # noqa: BLE001
            if srv.log.ErrorLog is not None:
                srv.log.ErrorLog.Printf("forced PR review %s failed: %v", title, err)
            srv._events.BUS.emit(
                "session.create_failed", session=title, data={"error": str(err)}
            )
            srv._cancel_intake_flight(
                title, "the session could not be created: %s" % err
            )
        finally:
            srv._pending_drop(title)

    srv._register_task(_bg_review())
    return {"started": True, "title": title}
