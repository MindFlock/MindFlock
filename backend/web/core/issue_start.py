"""Force-start issue sessions from the web UI (Intake → Issues).

The automated monitor (``backend.ticket_ingestion.issue_monitor``) only
picks up open issues that are (a) not authored by a skipped author, (b) past
the min-age grace period and (c) absent from state.json's
``processed_issues`` ledger — so an issue can silently never get handled
(most commonly: it was recorded processed/failed once). This module backs the
Intake → Issues tab: it lists every open issue on the watched repos
annotated with *why* auto handling is or isn't taking it, and force-starts a
session for any of them, bypassing those filters.

The forced path reuses the pipeline's own pieces (issue monitor, ticket
conversion, prompt builder, branch naming, the processed-issues ledger) but
runs inside the web server process, so it works even while ingestion is
stopped. ``server.py`` owns the engine side (registering the session in the
live grid) — mirroring :mod:`backend.web.core.pr_review` and
:mod:`backend.web.core.ticket_start`.
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


_REPO_ROOT = _resolve_repo_root()


def _load_config():
    """The pipeline's config, ``workspace_dir`` anchored at :data:`_REPO_ROOT`
    (:func:`backend.web.core.pipeline_root.load_config`)."""
    return pipeline_root.load_config(_REPO_ROOT)


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import) —
    see :func:`backend.web.core.pr_review._server`."""
    from backend.web import server

    return server


def session_title(issue) -> str:
    """Engine session title for an issue — the ticket slug the pipeline's own
    loop uses (``SessionRunner`` titles sessions by slug), so the panel's
    has-session check and pipeline-launched sessions collide correctly."""
    from backend.ticket_ingestion.models import issue_slug

    return f"issue-{issue_slug(issue)}"


def workspace_mode() -> str:
    """The engine workspace strategy issue sessions provision with
    (worktree/clone), matching ``SessionRunner``'s choice."""
    try:
        cfg = _load_config()
        return cfg.engine.mode if cfg.engine and cfg.engine.mode else "worktree"
    except Exception:  # noqa: BLE001
        return "worktree"


def skip_reasons(issue, processed: set, gh, now: datetime | None = None) -> list[str]:
    """Why the auto monitor would skip ``issue`` right now (empty = eligible).

    Mirrors the exact filters in ``IssueMonitor.scan`` so the UI explains the
    monitor's behavior instead of guessing at it.
    """
    reasons: list[str] = []
    if (issue.repo, issue.number) in processed:
        reasons.append("already handled before")
    # This repo's own skip list and grace period, which its card may override.
    skip = {a.strip().lower() for a in gh.issue_skip_authors_for(issue.repo) if a}
    if issue.author.lower() in skip:
        reasons.append(f"opened by {issue.author} — on your ignore list")
    now = now or datetime.now(timezone.utc)
    age_min = (now - issue.created_at).total_seconds() / 60.0
    min_age = gh.issue_min_age_for(issue.repo)
    if age_min < min_age:
        left = max(1, round(min_age - age_min))
        reasons.append(f"too new — waits {left} more min")
    return reasons


async def list_open_issues() -> dict:
    """Every open issue on the watched repos, annotated for the UI."""
    from backend.ticket_ingestion.issue_monitor import IssueMonitor
    from backend.ticket_ingestion.state import load_processed_issues

    cfg = _load_config()
    gh = cfg.github
    if gh is None or not gh.issue_repo_list():
        return {"repos": [], "enabled": False, "issues": []}

    monitor = IssueMonitor(gh)
    issues = []
    for repo in gh.issue_repo_list():
        issues.extend(await monitor._list_issues(repo))

    processed = load_processed_issues(_REPO_ROOT)
    now = datetime.now(timezone.utc)
    # App-wide, so resolved once for the listing (see the ticket panel's twin).
    strategy = workspace_mode()
    out = []
    for issue in sorted(issues, key=lambda i: i.created_at, reverse=True):
        reasons = skip_reasons(issue, processed, gh, now)
        out.append(
            {
                "repo": issue.repo,
                "number": issue.number,
                "title": issue.title,
                "url": issue.url,
                "author": issue.author,
                "created_at": issue.created_at.isoformat(),
                "session": session_title(issue),
                # What a run of this issue owns on disk — the branch and the
                # workspace strategy, for the reopen probe in server.py. The
                # repo URL is deliberately absent: an issue session provisions
                # from the default repository (``prepare_start`` never sets one
                # on its story), so the probe resolves the same default.
                "branch": branch_for(issue),
                "strategy": strategy,
                "eligible": not reasons,
                "reasons": reasons,
            }
        )
    return {
        "repos": gh.issue_repo_list(),
        "enabled": gh.issues_enabled,
        "issues": out,
    }


async def find_issue(repo: str, number: int):
    """The live GitHub record for one open issue (PRs excluded)."""
    from backend.ticket_ingestion.issue_monitor import IssueMonitor

    cfg = _load_config()
    if cfg.github is None:
        raise LookupError("Issue handling is not configured — add a repository first")
    monitor = IssueMonitor(cfg.github)
    for issue in await monitor._list_issues(repo):
        if issue.number == number:
            return issue
    raise LookupError(
        f"No open issue #{number} in {repo} — closed issues and pull "
        "requests can't be force-started"
    )


def branch_for(issue) -> str:
    """The branch a forced start will create for ``issue``.

    Pure and network-free (it only needs the issue's number/title), unlike
    :func:`prepare_start` — so the request path can hand it to the sidebar's
    provisioning row before the comments fetch has happened.
    """
    from backend.ticket_ingestion.issue_monitor import issue_to_ticket
    from backend.ticket_ingestion.provisioner import _branch_name_for

    return _branch_name_for(issue_to_ticket(issue, []))


async def prepare_start(issue) -> tuple:
    """Fetch the issue's comments and build its ticket + session prompt.

    Returns ``(story, prompt, branch)``. Network-bound (comments fetch) —
    run off the request path.
    """
    from backend.ticket_ingestion.claude_runner import ClaudeCodeRunner
    from backend.ticket_ingestion.issue_monitor import (
        IssueMonitor,
        issue_to_ticket,
    )
    from backend.ticket_ingestion.provisioner import _branch_name_for

    cfg = _load_config()
    comments = await IssueMonitor(cfg.github).fetch_comments(issue)
    story = issue_to_ticket(issue, comments)
    # This repo's own card first, then Intake → Issues' screen-wide Agent CLI
    # (``github.issue_agent``), then the ingestion-wide ``[mindflock].agent``;
    # "" = the app default. This used to call agent_for(), which skips straight
    # to the ingestion-wide value — so the Agent CLI picker on the issues screen
    # governed only the auto monitor and "Start work" launched whatever the app
    # default was.
    issue_agent = getattr(cfg, "issue_agent", None)
    story.agent = (
        issue_agent(getattr(issue, "repo", "")) if callable(issue_agent) else ""
    )
    prompt = ClaudeCodeRunner(cfg)._build_prompt(story, None, None)
    return story, prompt, _branch_name_for(story)


def record_handled(issue, error: str | None = None) -> None:
    """Ledger ``issue`` so auto handling doesn't run it a second time."""
    from backend.ticket_ingestion.models import ProcessedIssue
    from backend.ticket_ingestion.state import (
        load_processed_issues,
        record_processed_issue,
    )

    if (issue.repo, issue.number) in load_processed_issues(_REPO_ROOT):
        return
    record_processed_issue(
        _REPO_ROOT,
        ProcessedIssue(
            number=issue.number,
            processed_at=datetime.now(timezone.utc),
            repo=issue.repo,
            status="failed" if error else "",
        ),
    )


async def launch(
    repo: str,
    number: int,
    *,
    agent: str = "",
    depth: str = "",
    effort: str = "",
    plan: str = "",
) -> dict:
    """Start a coding session for one open issue and return the 202 body.

    The body of ``POST /api/github/issues/start`` (which validates its
    payload and calls this), callable without HTTP: ``POST /api/flights/start``
    with an ``issue`` subject calls it too. Same shape as the pipeline's issue
    loop (issue → Ticket → engine session on a fresh branch), against the
    server's engine; a launch failure is reported as ``session.create_failed``.

    * ``agent`` / ``depth`` / ``effort``: per-start overrides (``""`` = the
      repo card's chain).
    * ``plan``: the flight plan to fly; ``""`` = ``server._intake_plan``.

    Raises :class:`LaunchError` (404 no such open issue, 409 a session for it
    already exists, 502 GitHub failed).
    """
    srv = _server()
    agent_override = str(agent or "")
    effort_override = str(effort or "")
    # Row first, GitHub lookup second (see core.pr_review.launch).
    early = srv._cached_session_title(
        srv._OPEN_ISSUES_CACHE,
        "issues",
        lambda i: i.get("repo") == repo and i.get("number") == number,
    )
    if early:
        if early in srv.ENGINE.instances or srv._pending_has(early):
            raise LaunchError(
                409,
                {
                    "error": "session %s already exists — close it to re-run" % early,
                    "title": early,
                },
            )
        srv._pending_add(early, "iss", repo=repo)

    try:
        issue = await find_issue(repo, number)
    except LookupError as err:
        srv._pending_drop(early)
        raise LaunchError(404, {"error": str(err)}) from None
    except Exception as err:  # noqa: BLE001
        srv._pending_drop(early)
        raise LaunchError(502, {"error": str(err)}) from None

    title = session_title(issue)
    if title != early:
        srv._pending_drop(early)  # stale cache entry — keep only the real title
        if title in srv.ENGINE.instances or srv._pending_has(title):
            raise LaunchError(
                409,
                {
                    "error": "session %s already exists — close it to re-run" % title,
                    "title": title,
                },
            )
    srv._pending_add(
        title,
        "iss",
        branch=branch_for(issue),
        repo=repo,
        workspace_strategy=workspace_mode(),
    )
    repo_depth = srv._repo_intake_depth(repo, "issues")
    flight_tail = srv._arm_intake(
        title,
        depth or repo_depth,
        "iss",
        "%s#%s" % (repo, number),
        message=str(getattr(issue, "title", "") or ""),
        plan=srv._intake_plan(
            "issue", plan, depth, srv._repo_intake_plan(repo, "issues"), repo_depth
        ),
        subject={
            "kind": "issue",
            "source": repo,
            "id": str(number),
            "title": str(getattr(issue, "title", "") or ""),
            "url": str(getattr(issue, "url", "") or ""),
        },
        branch=branch_for(issue),
    )

    async def _bg_start() -> None:
        # The engine owns provisioning (inside Instance.Start), like the
        # ticket path.
        try:
            story, prompt, branch = await prepare_start(issue)
            # Same leftover-worktree reclaim as the ticket path.
            await asyncio.to_thread(
                srv._worktree_reclaim.reclaim_for_launch,
                getattr(story, "repo_url", "") or "",
                branch,
            )
            # This start's own pick outranks the repo card's Agent CLI, which
            # prepare_start already resolved onto the story. Resolved before the
            # options because this start's effort has to be translated into THAT
            # CLI's spelling.
            program = (
                agent_override
                or getattr(story, "agent", "")
                or srv.ENGINE.default_program()
            )
            prompt += flight_tail
            prompt = _provider_effort.decorate_prompt(prompt, program, effort_override)
            prompt = await asyncio.to_thread(
                lambda p=prompt: srv._red_zone_prompt(
                    p,
                    program,
                    srv._repo_url_workdirs(getattr(story, "repo_url", "") or ""),
                    None,
                )
            )
            inst = srv.session.NewInstance(
                srv.session.InstanceOptions(
                    title=title,
                    path=".",
                    program=program,
                    provisioned=True,
                    workspace_strategy=workspace_mode(),
                    new_branch=branch,
                    prompt=prompt,
                    provision_repo_url=getattr(story, "repo_url", "") or "",
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
                data={"program": inst.Program, "provisioned": True, "issue": number},
            )
            try:
                await asyncio.to_thread(inst.Start, True)
                srv.ENGINE.save()
            except Exception:
                # By identity: this task may be the loser of a re-start, and
                # popping by name would delete the LIVE session's record.
                srv._drop_failed_start(title, inst)
                raise
            # Ledger the issue so auto handling doesn't run it a second time.
            record_handled(issue)
            if srv.log.InfoLog is not None:
                srv.log.InfoLog.Printf("forced issue session %s live", title)
        except Exception as err:  # noqa: BLE001
            if srv.log.ErrorLog is not None:
                srv.log.ErrorLog.Printf(
                    "forced issue session %s failed: %v", title, err
                )
            srv._events.BUS.emit(
                "session.create_failed", session=title, data={"error": str(err)}
            )
            srv._cancel_intake_flight(
                title, "the session could not be created: %s" % err
            )
        finally:
            srv._pending_drop(title)

    srv._register_task(_bg_start())
    return {"started": True, "title": title}
