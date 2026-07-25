"""Poll the GitHub REST API for PRs that should be reviewed by Claude.

Selects open PRs in the configured repo that target the configured base branch
and are at least `min_age_minutes` old. Filters out (number, head_sha) pairs
that have already been processed.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from backend.ticket_ingestion.config import GithubConfig
from backend.ticket_ingestion.github_auth import resolve_token
from backend.ticket_ingestion.models import PullRequest
from backend.ticket_ingestion.state import load_processed_prs

_logger = logging.getLogger(__name__)
_STATE_DIR = Path(".")
# Total wall-clock budget for any single HTTP request/response.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
_GITHUB_API = "https://api.github.com"


class PRMonitor:
    def __init__(self, config: GithubConfig) -> None:
        self.config = config
        self._my_login: str | None = None

    async def scan(self) -> list[PullRequest]:
        repos = self.config.repo_list()
        if not repos:
            return []
        prs: list[PullRequest] = []
        for repo in repos:
            prs.extend(await self._list_prs(repo))
        if not prs:
            return []
        my_login = await self._authenticated_user_login()
        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=self.config.min_age_minutes
        )
        processed = load_processed_prs(_STATE_DIR)
        eligible = [
            pr
            for pr in prs
            if pr.author == my_login
            and pr.created_at <= cutoff
            and (pr.repo, pr.number) not in processed
        ]
        eligible.sort(key=lambda p: p.created_at)
        return eligible

    async def _authenticated_user_login(self) -> str:
        if self._my_login:
            return self._my_login
        token = await resolve_token(self.config)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(f"{_GITHUB_API}/user", headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"GitHub /user returned {resp.status}; cannot determine "
                        f"authenticated user for PR author filter."
                    )
                data = await resp.json()
        login = str(data.get("login", "")).strip()
        if not login:
            raise RuntimeError("GitHub /user returned no login.")
        self._my_login = login
        _logger.info("PR monitor restricted to PRs authored by %s", login)
        return login

    async def _list_prs(self, repo: str) -> list[PullRequest]:
        url = f"{_GITHUB_API}/repos/{repo}/pulls"
        params = {"state": "open", "per_page": "100"}
        # base_branch is an optional filter: set it to watch one branch across
        # every repo; leave it blank so repos with different default branches
        # (main vs master) all match.
        if self.config.base_branch:
            params["base"] = self.config.base_branch
        token = await resolve_token(self.config)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _logger.error(
                        "GitHub /pulls returned %d for %s: %s",
                        resp.status,
                        repo,
                        text[:200],
                    )
                    return []
                raw = await resp.json()

        out: list[PullRequest] = []
        for item in raw:
            try:
                # state=open is already filtered server-side; explicitly skip
                # drafts (the API treats them as open).
                if item.get("draft"):
                    continue
                if str(item.get("state", "")).lower() != "open":
                    continue
                base_repo = (item.get("base") or {}).get("repo") or {}
                clone_url = str(
                    base_repo.get("clone_url") or f"https://github.com/{repo}.git"
                )
                out.append(
                    PullRequest(
                        number=int(item["number"]),
                        head_ref=str(item["head"]["ref"]),
                        head_sha=str(item["head"]["sha"]),
                        base_ref=str(item["base"]["ref"]),
                        title=str(item.get("title", "")),
                        url=str(item.get("html_url", "")),
                        author=str((item.get("user") or {}).get("login", "")),
                        created_at=_parse_iso(str(item["created_at"])),
                        repo=repo,
                        clone_url=clone_url,
                    )
                )
            except (KeyError, ValueError, TypeError) as e:
                _logger.warning("Skipping malformed PR entry: %s", e)
        return out


def _parse_iso(s: str) -> datetime:
    ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts
