"""GitHub Issues provider — the zero-config on-ramp.

Ingests issues assigned to you in a single repository. This is the source that
should cost a new user nothing to connect, so **both** of its inputs resolve
themselves:

* **Credential** — the shared GitHub auth chain: an explicit
  ``ticketing.api_token``, else ``github.token`` in settings, else
  ``$GH_TOKEN``/``$GITHUB_TOKEN``, else ``gh auth token``. Anyone who has ever
  run ``gh auth login`` (or connected GitHub for PR review) is already done.
* **Repository** — :meth:`GithubIssuesProvider.resolve_repo` walks
  ``ticketing.project`` -> the source's own ``repo_url`` -> the global
  ``[repository].url`` -> this checkout's ``origin`` remote. So on a machine
  sitting in a GitHub clone, picking "GitHub Issues" and saving is the entire
  setup: no ``owner/repo`` to type, no token to paste.

Issue bodies are markdown, so the shared acceptance-criteria miner works
directly. Pull requests (which the issues endpoint also returns) are filtered out.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from backend.config.secrets import resolve_secret
from backend.ticket_ingestion.models import Ticket
from backend.ticket_ingestion.providers.base import (
    HTTP_TIMEOUT,
    ProviderError,
    TicketProvider,
    parse_acceptance_criteria,
    parse_iso8601,
)

_logger = logging.getLogger(__name__)
# Shared request budget (defined once in providers/base.py).
_HTTP_TIMEOUT = HTTP_TIMEOUT
_API = "https://api.github.com"
_MAX_ISSUES = 50


async def _gh_auth_token() -> str | None:
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "auth",
            "token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return (stdout.decode(errors="replace").strip()) or None


class GithubIssuesProvider(TicketProvider):
    name = "github_issues"
    label = "GitHub"
    slug_prefix = "gh"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._token: str | None = None

    async def _resolve_token(self) -> str:
        if self._token:
            return self._token
        token = await resolve_secret(
            explicit=self.cfg.api_token,
            settings_getter=lambda s: s.github.token,
            env_vars=("GH_TOKEN", "GITHUB_TOKEN"),
            cli_fallback=_gh_auth_token,
        )
        if not token:
            raise ProviderError(
                "No GitHub token available — set this source's Token on its "
                "card (Intake → Tickets), paste one under Intake → Pull requests → "
                "Advanced options, export GH_TOKEN, or run `gh auth login`."
            )
        self._token = token
        return token

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._resolve_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def resolve_repo(self) -> str:
        """The ``owner/repo`` to ingest from, or ``""`` if nothing names one.

        Tries, in order: an explicit ``project``; this source's ``repo_url``
        (already required, so a configured source almost always resolves here);
        the global ``[repository].url``; and finally this checkout's ``origin``
        remote — which is what makes "install, pick GitHub Issues, save" work
        with no repo typed anywhere.

        Every step is best-effort and any failure just falls through to the next,
        because a resolution error here must read as "tell me the repo", not as a
        crash during a poll.
        """
        explicit = (self.cfg.project or "").strip().strip("/")
        if explicit:
            return explicit
        from backend.session.git.remote_url import parse_remote

        for url in (self._config_repo_url(), self._origin_url()):
            ref = parse_remote(url)
            if ref is not None:
                return ref.slug
        return ""

    def _config_repo_url(self) -> str:
        """This source's clone URL, else the globally configured one."""
        if (self.cfg.repo_url or "").strip():
            return self.cfg.repo_url.strip()
        try:
            from backend.config import settings as _settings

            return (_settings.load_settings().repository.url or "").strip()
        except Exception:  # noqa: BLE001 — settings are optional
            return ""

    def _origin_url(self) -> str:
        """This checkout's ``origin`` remote, or ``""``.

        The last resort in :meth:`resolve_repo`. Short timeout and every failure
        swallowed: not being in a git repo is an ordinary outcome here.
        """
        import subprocess

        try:
            out = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    def _repo(self) -> tuple[str, str]:
        slug = self.resolve_repo()
        parts = slug.split("/")
        if len(parts) != 2 or not all(parts):
            raise ProviderError(
                "github_issues could not work out which repository to read. Set "
                "the source's Repo URL (or its Repository field to 'owner/repo'), "
                "or run MindFlock from a GitHub clone so `origin` can be used."
            )
        return parts[0], parts[1]

    async def _login(self, session: aiohttp.ClientSession, headers: dict) -> str:
        if self.cfg.member_id:
            return self.cfg.member_id
        async with session.get(f"{_API}/user", headers=headers) as resp:
            if resp.status != 200:
                raise ProviderError(f"GitHub /user returned HTTP {resp.status}")
            me = await resp.json()
        return str(me.get("login") or "")

    async def _fetch_comments(
        self, session: aiohttp.ClientSession, headers: dict, url: str
    ) -> list[str]:
        if not url:
            return []
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return []
                items = await resp.json()
        except aiohttp.ClientError:
            return []
        out = []
        for c in items or []:
            body = (c.get("body") or "").strip()
            if not body:
                continue
            author = (c.get("user") or {}).get("login") or "unknown"
            out.append(f"[{c.get('created_at') or ''} by {author}] {body}")
        return out

    async def _issue_to_ticket(
        self, session: aiohttp.ClientSession, headers: dict, issue: dict[str, Any]
    ) -> Ticket:
        number = issue.get("number")
        body = issue.get("body") or ""
        comments = []
        if issue.get("comments"):
            comments = await self._fetch_comments(
                session, headers, issue.get("comments_url")
            )
        assignees = [
            a.get("login") for a in (issue.get("assignees") or []) if a.get("login")
        ]
        return Ticket(
            id=number,
            name=str(issue.get("title") or ""),
            description=body,
            acceptance_criteria=parse_acceptance_criteria(body),
            owner_ids=[str(a) for a in assignees],
            app_url=issue.get("html_url") or "",
            created_at=parse_iso8601(issue.get("created_at")),
            comments=comments,
            attachments=[],  # GitHub inlines images as markdown links in the body
            provider="github_issues",
            slug=self.make_slug(number),
            source_label=self.label,
        )

    async def search_assigned(self, since: datetime) -> list[Ticket]:
        owner, repo = self._repo()
        headers = await self._headers()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            login = await self._login(session, headers)
            params = {
                "assignee": login or "*",
                "state": "open",
                "sort": "updated",
                "direction": "desc",
                "since": since.isoformat(),
                "per_page": str(_MAX_ISSUES),
            }
            async with session.get(
                f"{_API}/repos/{owner}/{repo}/issues", params=params, headers=headers
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise aiohttp.ClientError(
                        f"GitHub API returned {resp.status}: {text[:200]}"
                    )
                issues = await resp.json()
            tickets = []
            for issue in issues or []:
                if issue.get("pull_request"):
                    continue  # the issues endpoint also lists PRs
                tickets.append(await self._issue_to_ticket(session, headers, issue))
        return tickets

    async def fetch(self, ticket_id: str) -> Ticket:
        owner, repo = self._repo()
        headers = await self._headers()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(
                f"{_API}/repos/{owner}/{repo}/issues/{ticket_id}", headers=headers
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        f"GitHub API returned {resp.status} for issue {ticket_id}: {text[:200]}"
                    )
                issue = await resp.json()
            return await self._issue_to_ticket(session, headers, issue)

    async def test_connection(self) -> tuple[dict | None, str]:
        try:
            headers = await self._headers()
            owner, repo = self._repo()  # resolve + validate the scope early
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.get(f"{_API}/user", headers=headers) as resp:
                    if resp.status in (401, 403):
                        return None, f"GitHub rejected the token (HTTP {resp.status})"
                    if resp.status != 200:
                        return None, f"GitHub API returned HTTP {resp.status}"
                    me = await resp.json()
        except ProviderError as e:
            return None, str(e)
        except aiohttp.ClientError as e:
            return None, f"network error reaching GitHub: {e}"
        # ``project`` rides back so the UI can show (and store) the repo it
        # auto-detected — the user sees what "zero config" actually resolved to
        # instead of an empty field they have to trust.
        return {
            "member_id": str(me.get("login", "")),
            "name": me.get("name"),
            "project": f"{owner}/{repo}",
        }, ""
