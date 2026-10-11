"""GitHub Issues provider — the zero-config on-ramp.

Ingests issues assigned to you in a single repository. This is the source that
should cost a new user nothing to connect, so **both** of its inputs resolve
themselves:

* **Credential** — the one GitHub chain (:mod:`backend.ticket_ingestion.
  github_auth`): Connect GitHub's ``github.token``, else
  ``$GH_TOKEN``/``$GITHUB_TOKEN``, else ``gh auth token``. Anyone who has ever
  run ``gh auth login`` (or connected GitHub) is already done. A per-source
  ``api_token`` from an older release is still honoured as the LAST fallback
  for one release, with a notice to move it to Connect GitHub; the field is
  no longer offered on the card.
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

from backend.ticket_ingestion import github_auth as _github_auth
from backend.ticket_ingestion.models import Ticket
from backend.ticket_ingestion.providers.base import (
    HTTP_TIMEOUT,
    ProviderError,
    TicketNotFound,
    TicketProvider,
    ingests_any_assignee,
    parse_acceptance_criteria,
    parse_iso8601,
)

_logger = logging.getLogger(__name__)
# Shared request budget (defined once in providers/base.py).
_HTTP_TIMEOUT = HTTP_TIMEOUT
_API = "https://api.github.com"
_MAX_ISSUES = 50


#: Said once per process: a per-source token still in settings works, but it
#: is the deprecated path and goes away next release.
_PER_SOURCE_TOKEN_NOTICED = False
PER_SOURCE_TOKEN_NOTICE = (
    "GitHub Issues source still has its own Token (deprecated): move it to "
    "Settings → Connections → GitHub → Connect. It is read as a last fallback "
    "this release and ignored from the next."
)


class GithubIssuesProvider(TicketProvider):
    name = "github_issues"
    label = "GitHub"
    slug_prefix = "gh"

    def __init__(self, cfg) -> None:
        super().__init__(cfg)

    async def _resolve_token(self) -> str:
        """The shared chain (memoized a minute by github_auth, so a token
        pasted into Connect GitHub reaches the next poll — no per-provider
        cache for the life of the process any more), then the deprecated
        per-source ``api_token`` as the last fallback."""
        global _PER_SOURCE_TOKEN_NOTICED
        per_source = (self.cfg.api_token or "").strip()
        if per_source and not _PER_SOURCE_TOKEN_NOTICED:
            _PER_SOURCE_TOKEN_NOTICED = True
            _logger.warning(PER_SOURCE_TOKEN_NOTICE)
        try:
            return await _github_auth.resolve_token(None)
        except _github_auth.GithubAuthError:
            if per_source:
                return per_source
            raise ProviderError(
                "No GitHub token available — connect GitHub under Settings → "
                "Connections → GitHub → Connect, export GH_TOKEN, or run "
                "`gh auth login`."
            )

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
            owner_names=[str(a) for a in assignees],  # login is the display name
            app_url=issue.get("html_url") or "",
            created_at=parse_iso8601(issue.get("created_at")),
            comments=comments,
            attachments=[],  # GitHub inlines images as markdown links in the body
            provider="github_issues",
            slug=self.make_slug(number),
            source_label=self.label,
            labels=[
                str(lab.get("name"))
                for lab in issue.get("labels") or []
                if isinstance(lab, dict) and lab.get("name")
            ],
        )

    async def search_assigned(self, since: datetime) -> list[Ticket]:
        owner, repo = self._repo()
        headers = await self._headers()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            params = {
                "state": "open",
                "sort": "updated",
                "direction": "desc",
                "since": since.isoformat(),
                "per_page": str(_MAX_ISSUES),
            }
            # "anyone" omits the assignee filter entirely — including unassigned
            # issues, which is what a triage queue wants. There are no workflow
            # states here to bound it, so the repo is the bound.
            if not ingests_any_assignee(self.cfg):
                login = await self._login(session, headers)
                if not login:
                    # Historically this fell through to assignee="*" — an "any
                    # assignee" search wearing an "assigned to me" label. Now
                    # that widening the scope is a setting, say so instead of
                    # silently ingesting the whole repo's issues.
                    raise ProviderError(
                        "Could not resolve your GitHub login, so 'assigned to "
                        "me' has nothing to match — set the source's Member ID, "
                        "or switch it to Anyone's tickets on purpose."
                    )
                params["assignee"] = login
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
                    # 410 Gone is what GitHub answers for a deleted issue.
                    gone = resp.status in (404, 410)
                    raise (TicketNotFound if gone else ProviderError)(
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

    # ----------------------------------------------------------------- #
    # Writes (Intake → Tickets → Merge into…). See TicketProvider for the
    # contract and for why the order in ticket_merge.py is the order it is.
    #
    # ``carry_attachments`` is deliberately NOT overridden. GitHub has no
    # attachment API at all: an image or file dropped into an issue is uploaded
    # to githubusercontent / user-attachments and referenced from the body as a
    # markdown link, owned by the uploader rather than by the issue. Those links
    # keep resolving after the issue is deleted, so copying the body IS carrying
    # the files, and the base class's "nothing to move" default is the truth.
    # ----------------------------------------------------------------- #
    can_merge = True

    async def _issue_json(self, ticket_id: str) -> dict[str, Any]:
        owner, repo = self._repo()
        headers = await self._headers()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(
                f"{_API}/repos/{owner}/{repo}/issues/{ticket_id}", headers=headers
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        f"GitHub API returned {resp.status} for issue "
                        f"{ticket_id}: {text[:200]}"
                    )
                return await resp.json()

    async def append_description(self, ticket_id: str, addition: str) -> None:
        owner, repo = self._repo()
        headers = await self._headers()
        current = (await self._issue_json(ticket_id)).get("body") or ""
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.patch(
                f"{_API}/repos/{owner}/{repo}/issues/{ticket_id}",
                json={"body": current + addition},
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        f"GitHub could not update issue {ticket_id} "
                        f"(HTTP {resp.status}): {text[:200]}"
                    )

    # --- claim mirror (tracker_claims) -------------------------------- #

    can_claim = True

    async def claim_identity(self) -> str:
        """The token's login (``member_id`` when the source names one)."""
        headers = await self._headers()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            return await self._login(session, headers)

    async def _issue_call(
        self, method: str, suffix: str, body: dict | None, ok: tuple, what: str
    ) -> None:
        owner, repo = self._repo()
        headers = await self._headers()
        url = f"{_API}/repos/{owner}/{repo}/issues/{suffix}"
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            call = session.delete if method == "DELETE" else session.post
            kwargs = {"headers": headers}
            if body is not None:
                kwargs["json"] = body
            async with call(url, **kwargs) as resp:
                if resp.status not in ok:
                    text = await resp.text()
                    raise ProviderError(
                        f"GitHub could not {what} (HTTP {resp.status}): {text[:200]}"
                    )

    async def assign_to(self, ticket_id: str, member: str) -> None:
        member = (member or "").strip()
        if not member:
            raise ProviderError("no GitHub login to assign")
        await self._issue_call(
            "POST",
            f"{ticket_id}/assignees",
            {"assignees": [member]},
            (200, 201),
            f"assign issue {ticket_id}",
        )

    async def add_label(self, ticket_id: str, label: str) -> None:
        # GitHub creates a label it doesn't know yet on the way.
        await self._issue_call(
            "POST",
            f"{ticket_id}/labels",
            {"labels": [label]},
            (200, 201),
            f"label issue {ticket_id}",
        )

    async def remove_label(self, ticket_id: str, label: str) -> None:
        from urllib.parse import quote

        # 404 = the label is not on the issue: already done.
        await self._issue_call(
            "DELETE",
            f"{ticket_id}/labels/{quote(label, safe='')}",
            None,
            (200, 204, 404),
            f"unlabel issue {ticket_id}",
        )

    async def add_comment(self, ticket_id: str, body: str) -> None:
        owner, repo = self._repo()
        headers = await self._headers()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                f"{_API}/repos/{owner}/{repo}/issues/{ticket_id}/comments",
                json={"body": body},
                headers=headers,
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise ProviderError(
                        f"GitHub could not comment on issue {ticket_id} "
                        f"(HTTP {resp.status}): {text[:200]}"
                    )

    async def delete_ticket(self, ticket_id: str) -> None:
        """Delete the issue through the GraphQL ``deleteIssue`` mutation.

        The REST API cannot do this — there is no ``DELETE /issues/{n}``, which
        is why this is the one adapter here that reaches for GraphQL — and the
        mutation is gated on admin rights over the repository. That is a real
        constraint on a real feature, so the failure says which permission is
        missing instead of returning a bare 403: everything the merge copied is
        already on the surviving issue by the time this runs, and the caller
        reports "merged, but not deleted" rather than pretending otherwise.
        """
        node_id = (await self._issue_json(ticket_id)).get("node_id")
        if not node_id:
            raise ProviderError(
                f"GitHub did not report a node id for issue {ticket_id}"
            )
        headers = await self._headers()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                f"{_API}/graphql",
                json={
                    "query": (
                        "mutation($id: ID!) {"
                        " deleteIssue(input: { issueId: $id })"
                        " { clientMutationId } }"
                    ),
                    "variables": {"id": node_id},
                },
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        f"GitHub refused to delete issue {ticket_id} "
                        f"(HTTP {resp.status}): {text[:200]}"
                    )
                payload = await resp.json()
        errors = payload.get("errors") or []
        if errors:
            message = "; ".join(str(e.get("message") or e) for e in errors)
            raise ProviderError(
                f"GitHub refused to delete issue {ticket_id}: {message} — "
                "deleting an issue needs admin permission on the repository, "
                "and a fine-grained token needs its Issues scope set to "
                "read and write."
            )

    # --- filing a new ticket (New → Ticket) -------------------------- #

    can_create = True

    def create_blocker(self) -> str:
        """Refuse before the model turn when no repository resolves.

        :meth:`_repo` raises the same thing, but it raises at file time — after
        a ~20s draft the user has already waited for. This is the same question
        asked for free, and the compose route asks it first.
        """
        try:
            self._repo()
        except ProviderError as err:
            return str(err)
        return ""

    async def create_ticket(self, name: str, description: str) -> Ticket:
        """File a new issue (``POST /repos/{owner}/{repo}/issues``).

        Assigned to the configured member (or, failing that, whoever the token
        belongs to) for the reason every adapter here assigns: ``search_assigned``
        looks the ticket up BY assignee, so an unassigned issue is one this
        source can never list again.

        The assignment is best-effort and separated from the body: a token
        without push rights cannot assign anyone, and GitHub answers that by
        silently dropping the field rather than failing — so the issue still
        gets filed, and the ``assignees`` the response carries are what the
        returned Ticket reports. An issue that exists and is unassigned is a
        far better outcome than a refusal.
        """
        owner, repo = self._repo()
        headers = await self._headers()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            body: dict[str, Any] = {"title": name, "body": description}
            try:
                login = await self._login(session, headers)
            except ProviderError:
                login = ""
            if login:
                body["assignees"] = [login]
            async with session.post(
                f"{_API}/repos/{owner}/{repo}/issues", json=body, headers=headers
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise ProviderError(
                        f"GitHub could not create an issue on {owner}/{repo} "
                        f"(HTTP {resp.status}): {text[:200]}"
                    )
                issue = await resp.json()
            return await self._issue_to_ticket(session, headers, issue)
