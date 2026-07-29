"""Force-start issue sessions from the web UI (Settings → Git issues).

The automated monitor (``backend.ticket_ingestion.issue_monitor``) only
picks up open issues that are (a) not authored by a skipped author, (b) past
the min-age grace period and (c) absent from state.json's
``processed_issues`` ledger — so an issue can silently never get handled
(most commonly: it was recorded processed/failed once). This module backs the
Settings → Git issues screen: it lists every open issue on the watched repos
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

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

_logger = logging.getLogger(__name__)


def _resolve_repo_root() -> Path:
    """Where the pipeline keeps state.json / workspaces/ — same resolution
    order as the ingestion addon and :mod:`backend.web.core.pr_review`:
    ``MINDFLOCK_REPO_ROOT`` env → nearest ancestor with ``config.toml`` → cwd.
    """
    env = (os.environ.get("MINDFLOCK_REPO_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "config.toml").is_file():
            return parent
    return Path.cwd()


_REPO_ROOT = _resolve_repo_root()


def _load_config():
    """The pipeline's layered config (env → settings.json → config.toml),
    with the relative ``workspace_dir`` default re-anchored at the repo root
    (this server's cwd is not guaranteed to match the pipeline's)."""
    from backend.ticket_ingestion.config import load_config

    cfg = load_config()
    if not Path(cfg.workspace_dir).is_absolute():
        cfg.workspace_dir = _REPO_ROOT / cfg.workspace_dir
    return cfg


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
        reasons.append("already handled (recorded in the processed ledger)")
    skip = {a.strip().lower() for a in (gh.issue_skip_authors or []) if a}
    if issue.author.lower() in skip:
        reasons.append(f"authored by {issue.author} (in the skip list)")
    now = now or datetime.now(timezone.utc)
    age_min = (now - issue.created_at).total_seconds() / 60.0
    if age_min < gh.issue_min_age_minutes:
        left = max(1, round(gh.issue_min_age_minutes - age_min))
        reasons.append(f"in the min-age grace period ({left} min left)")
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
