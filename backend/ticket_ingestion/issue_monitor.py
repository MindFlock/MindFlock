"""Poll the GitHub REST API for newly opened issues that should be worked on.

Sibling of :mod:`pr_monitor`: selects open issues in the configured repos that
are at least ``min_age_minutes`` old, skipping authors in ``skip_authors`` and
``(repo, number)`` pairs already recorded in the processed-issues ledger. The
issues endpoint also returns pull requests — those are filtered out.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from backend.ticket_ingestion.clone_transport import (
    configured_repo_url,
    resolve_clone_url,
    resolve_transport,
)
from backend.ticket_ingestion.config import GithubConfig
from backend.ticket_ingestion.github_auth import resolve_token
from backend.ticket_ingestion.models import Issue
from backend.ticket_ingestion.state import load_processed_issues

_logger = logging.getLogger(__name__)
_STATE_DIR = Path(".")
# Total wall-clock budget for any single HTTP request/response.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
_GITHUB_API = "https://api.github.com"


class IssueCommentsFetchError(Exception):
    """A transient failure fetching an issue's comments (retry next poll)."""


class IssueMonitor:
    def __init__(self, config: GithubConfig) -> None:
        self.config = config

    async def scan(self) -> list[Issue]:
        """Open issues across the configured repos that are eligible to work on.

        Lists every configured repo, then keeps only issues whose author is not
        in ``issue_skip_authors``, that are at least ``issue_min_age_minutes``
        old, and that are not already in the processed-issues ledger. Returns
        them oldest-first (empty list when no repos or no eligible issues).
        """
        repos = self.config.issue_repo_list()
        if not repos:
            return []
        issues: list[Issue] = []
        for repo in repos:
            issues.extend(await self._list_issues(repo))
        if not issues:
            return []
        # Grace period and skip list are both per-repo (each watched repo is its
        # own card in the Intake tab), so they are resolved per issue. `now` is
        # taken once so a slow sweep can't move the cutoff mid-scan.
        now = datetime.now(timezone.utc)
        processed = load_processed_issues(_STATE_DIR)

        def _skipped(issue) -> bool:
            skip = {
                a.strip().lower()
                for a in self.config.issue_skip_authors_for(issue.repo)
                if a
            }
            return issue.author.lower() in skip

        eligible = [
            issue
            for issue in issues
            if not _skipped(issue)
            and issue.created_at
            <= now - timedelta(minutes=self.config.issue_min_age_for(issue.repo))
            and (issue.repo, issue.number) not in processed
        ]
        eligible.sort(key=lambda i: i.created_at)
        return eligible

    async def _headers(self) -> dict[str, str]:
        token = await resolve_token(self.config)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _list_issues(self, repo: str) -> list[Issue]:
        url = f"{_GITHUB_API}/repos/{repo}/issues"
        params = {"state": "open", "per_page": "100"}
        headers = await self._headers()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _logger.error(
                        "GitHub /issues returned %d for %s: %s",
                        resp.status,
                        repo,
                        text[:200],
                    )
                    return []
                raw = await resp.json()

        # Where a session for these issues will clone from. The issues endpoint
        # only knows the repo by its owner/name slug, so the transport is a
        # choice rather than a given: honour the user's own [repository].url
        # spelling (SSH or HTTPS) instead of hardcoding an HTTPS URL that an
        # SSH-only checkout has no credential for. Per-item `repository` data is
        # preferred when the payload carries it (cross-repo issue feeds do).
        transport = resolve_transport(self.config)
        configured = configured_repo_url(self.config)
        default_clone_url = resolve_clone_url(
            repo, configured_url=configured, transport=transport
        )
        out: list[Issue] = []
        for item in raw:
            try:
                # The issues endpoint also lists PRs — skip them.
                if item.get("pull_request"):
                    continue
                if str(item.get("state", "")).lower() != "open":
                    continue
                item_repo = item.get("repository") or {}
                clone_url = (
                    resolve_clone_url(
                        repo,
                        api_https=item_repo.get("clone_url"),
                        api_ssh=item_repo.get("ssh_url"),
                        configured_url=configured,
                        transport=transport,
                    )
                    if item_repo
                    else default_clone_url
                )
                out.append(
                    Issue(
                        number=int(item["number"]),
                        title=str(item.get("title", "")),
                        body=str(item.get("body") or ""),
                        url=str(item.get("html_url", "")),
                        author=str((item.get("user") or {}).get("login", "")),
                        created_at=_parse_iso(str(item["created_at"])),
                        repo=repo,
                        clone_url=clone_url,
                    )
                )
            except (KeyError, ValueError, TypeError) as e:
                _logger.warning("Skipping malformed issue entry: %s", e)
        return out

    async def fetch_comments(self, issue: Issue) -> list[str]:
        """All comments on ``issue``, rendered ``"[<ts> by <author>] <body>"``.

        Raises :class:`IssueCommentsFetchError` on any HTTP/network failure so
        the caller retries next poll instead of starting a session with the
        discussion silently missing.
        """
        url = f"{_GITHUB_API}/repos/{issue.repo}/issues/{issue.number}/comments"
        headers = await self._headers()
        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.get(
                    url, params={"per_page": "100"}, headers=headers
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise IssueCommentsFetchError(
                            f"GitHub /issues/{issue.number}/comments returned "
                            f"{resp.status}: {text[:200]}"
                        )
                    items = await resp.json()
        except aiohttp.ClientError as e:
            raise IssueCommentsFetchError(str(e)) from e
        out: list[str] = []
        for c in items or []:
            body = (c.get("body") or "").strip()
            if not body:
                continue
            author = (c.get("user") or {}).get("login") or "unknown"
            out.append(f"[{c.get('created_at') or ''} by {author}] {body}")
        return out


def issue_to_ticket(issue: Issue, comments: list[str]):
    """Present ``issue`` as a normalized :class:`Ticket` so the whole ticket
    path (prompt builder, provisioner, engine bridge) handles it unchanged —
    the session gets a fresh ``feature/issue-<repo>-<n>/<title>`` branch.
    Shared by the pipeline's issue loop and the web UI's force-start."""
    from backend.ticket_ingestion.models import Ticket, issue_slug
    from backend.ticket_ingestion.providers.base import parse_acceptance_criteria

    return Ticket(
        id=issue.number,
        name=issue.title,
        description=issue.body or issue.title,
        acceptance_criteria=parse_acceptance_criteria(issue.body or ""),
        owner_ids=[],
        app_url=issue.url,
        created_at=issue.created_at,
        comments=comments,
        provider="github_issues",
        slug=f"issue-{issue_slug(issue)}",
        source_label="GitHub Issue",
        repo_url=issue.clone_url,
    )


def _parse_iso(s: str) -> datetime:
    ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts
