"""Jira Cloud provider.

Uses the current enhanced-search endpoint ``POST /rest/api/3/search/jql`` (the
legacy ``/rest/api/3/search`` was removed from Jira Cloud) with a JQL of
``assignee = currentUser() AND updated >= "<since>"``, then hydrates each issue
for its description (Atlassian Document Format, flattened to text), comments and
attachments.

Auth: HTTP Basic with the account email + an API token
(https://id.atlassian.com/manage-profile/security/api-tokens). ``base_url`` is
the site, e.g. ``https://your-domain.atlassian.net``.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any

import aiohttp

from backend.ticket_ingestion.models import Attachment, Ticket
from backend.ticket_ingestion.providers.base import (
    HTTP_TIMEOUT,
    ProviderError,
    TicketProvider,
    parse_acceptance_criteria,
    parse_iso8601,
    workflow_state_list,
)

_logger = logging.getLogger(__name__)
# Shared request budget (defined once in providers/base.py).
_HTTP_TIMEOUT = HTTP_TIMEOUT
_MAX_ISSUES = 50


def flatten_adf(node: Any) -> str:
    """Flatten an Atlassian Document Format tree into readable plain text.

    Preserves paragraph/line breaks and bullet markers so the acceptance-criteria
    miner (which keys on ``-`` bullets and headings) still works.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten_adf(n) for n in node)
    if not isinstance(node, dict):
        return ""

    ntype = node.get("type")
    if ntype == "text":
        return str(node.get("text") or "")
    if ntype == "hardBreak":
        return "\n"
    if ntype == "mention":
        return "@" + str((node.get("attrs") or {}).get("text") or "")

    inner = flatten_adf(node.get("content"))
    if ntype in ("paragraph", "heading"):
        return inner + "\n"
    if ntype == "listItem":
        return "- " + inner.strip() + "\n"
    if ntype in ("bulletList", "orderedList"):
        return inner
    if ntype == "codeBlock":
        return inner + "\n"
    return inner


class JiraProvider(TicketProvider):
    name = "jira"
    label = "Jira"
    slug_prefix = "jira"

    def _headers(self) -> dict[str, str]:
        raw = f"{self.cfg.email}:{self.cfg.api_token}".encode("utf-8")
        return {
            "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _api(self, path: str) -> str:
        return f"{self.cfg.base_url.rstrip('/')}{path}"

    def _issue_to_ticket(self, issue: dict[str, Any]) -> Ticket:
        key = str(issue.get("key") or issue.get("id") or "")
        fields = issue.get("fields") or {}
        description = (
            flatten_adf(fields.get("description")) if fields.get("description") else ""
        )

        comments = []
        for c in (fields.get("comment") or {}).get("comments") or []:
            body = flatten_adf(c.get("body")).strip()
            if not body:
                continue
            author = (c.get("author") or {}).get("displayName") or "unknown"
            created = c.get("created") or ""
            comments.append(f"[{created} by {author}] {body}")

        attachments: list[Attachment] = []
        for a in fields.get("attachment") or []:
            url = a.get("content")
            if not url:
                continue
            attachments.append(
                Attachment(
                    name=a.get("filename") or "attachment",
                    url=url,
                    content_type=a.get("mimeType"),
                    auth_headers={"Authorization": self._headers()["Authorization"]},
                )
            )

        assignee = fields.get("assignee") or {}
        browse = (
            f"{self.cfg.base_url.rstrip('/')}/browse/{key}"
            if key
            else self.cfg.base_url
        )
        return Ticket(
            id=key,
            name=str(fields.get("summary") or ""),
            description=description,
            acceptance_criteria=parse_acceptance_criteria(description),
            owner_ids=(
                [str(assignee.get("accountId"))] if assignee.get("accountId") else []
            ),
            app_url=browse,
            created_at=parse_iso8601(fields.get("created")),
            comments=comments,
            attachments=attachments,
            provider="jira",
            slug=self.make_slug(key),
            source_label=self.label,
        )

    async def search_assigned(self, since: datetime) -> list[Ticket]:
        states = workflow_state_list(self.cfg)
        state_clause = ""
        if states:
            # Numeric = a status id; otherwise a status name (quoted for JQL).
            # Several configured ingest states become one IN clause.
            quoted = ", ".join(s if s.isdigit() else f'"{s}"' for s in states)
            state_clause = f" AND status IN ({quoted})"
        jql = (
            f'assignee = currentUser() AND updated >= "{since.strftime("%Y-%m-%d %H:%M")}"'
            f"{state_clause} ORDER BY updated DESC"
        )
        body = {
            "jql": jql,
            "maxResults": _MAX_ISSUES,
            "fields": [
                "summary",
                "description",
                "comment",
                "attachment",
                "assignee",
                "created",
            ],
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                self._api("/rest/api/3/search/jql"), json=body, headers=self._headers()
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise aiohttp.ClientError(
                        f"Jira API returned {resp.status}: {text[:200]}"
                    )
                data = await resp.json()
        return [self._issue_to_ticket(i) for i in (data.get("issues") or [])]

    async def fetch(self, ticket_id: str) -> Ticket:
        params = {"fields": "summary,description,comment,attachment,assignee,created"}
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(
                self._api(f"/rest/api/3/issue/{ticket_id}"),
                params=params,
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        f"Jira API returned {resp.status} for issue {ticket_id}: {text[:200]}"
                    )
                data = await resp.json()
        return self._issue_to_ticket(data)

    async def test_connection(self) -> tuple[dict | None, str]:
        if not self.cfg.base_url:
            return None, "no Jira site URL configured (e.g. https://you.atlassian.net)"
        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.get(
                    self._api("/rest/api/3/myself"), headers=self._headers()
                ) as resp:
                    if resp.status in (401, 403):
                        return (
                            None,
                            f"Jira rejected the credentials (HTTP {resp.status})",
                        )
                    if resp.status != 200:
                        return None, f"Jira API returned HTTP {resp.status}"
                    me = await resp.json()
        except aiohttp.ClientError as e:
            return None, f"network error reaching Jira: {e}"
        return {
            "member_id": str(me.get("accountId", "")),
            "name": me.get("displayName"),
        }, ""

    async def list_states(self) -> list[dict]:
        """All Jira statuses on the site (``GET /rest/api/3/status``). Stored id
        is the numeric status id used in the JQL ``status = <id>`` filter."""
        if not self.cfg.base_url:
            raise ProviderError("no Jira site URL configured")
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(
                self._api("/rest/api/3/status"), headers=self._headers()
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        f"Jira /status returned HTTP {resp.status}: {text[:200]}"
                    )
                statuses = await resp.json()
        seen: set[str] = set()
        out: list[dict] = []
        for s in statuses or []:
            sid = str(s.get("id") or "")
            name = s.get("name") or sid
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append({"id": sid, "name": name})
        return out
