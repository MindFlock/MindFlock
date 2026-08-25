"""Jira Cloud provider.

Uses the current enhanced-search endpoint ``POST /rest/api/3/search/jql`` (the
legacy ``/rest/api/3/search`` was removed from Jira Cloud) with a JQL of
``assignee = currentUser() AND updated >= "<since>"`` for the pipeline poll and
the bare ``assignee = currentUser()`` for the UI's assigned-tickets panel, then
hydrates each issue for its description (Atlassian Document Format, flattened to
markdown-ish text), comments and attachments.

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
    ingests_any_assignee,
    parse_acceptance_criteria,
    parse_iso8601,
    workflow_state_list,
)

_logger = logging.getLogger(__name__)
# Shared request budget (defined once in providers/base.py).
_HTTP_TIMEOUT = HTTP_TIMEOUT
_MAX_ISSUES = 50
# Deepest markdown heading level; ADF headings are 1-6.
_MAX_HEADING_LEVEL = 6

# Issue fields every code path needs: the prompt content (summary/description/
# comments/attachments), the assignee filter, and ``status`` — the human-readable
# bucket the assigned-tickets panel groups by (:attr:`Ticket.state`).
_ISSUE_FIELDS = (
    "summary",
    "description",
    "comment",
    "attachment",
    "assignee",
    "created",
    "status",
)

# Jira status categories -> the workflow-state ``type`` vocabulary the
# assigned-tickets panel shares with the Shortcut adapter
# (``unstarted`` | ``started`` | ``done``). Jira's category keys are "new"
# (To Do), "indeterminate" (In Progress) and "done".
_STATUS_CATEGORY_TYPES = {
    "new": "unstarted",
    "indeterminate": "started",
    "done": "done",
}


def flatten_adf(node: Any) -> str:
    """Flatten an Atlassian Document Format tree into markdown-ish plain text.

    Paragraph/line breaks, ``-`` bullet markers AND ``#`` heading markers all
    have to survive, because :func:`parse_acceptance_criteria` is line-oriented:
    it only enters its acceptance-criteria section for a line matching
    ``^#+ acceptance criteria$`` and otherwise falls back to treating *every*
    top-level bullet in the description as a criterion. Dropping the heading
    markers therefore does not degrade gracefully — it silently mines the
    ticket's context bullets instead of its acceptance criteria.
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
    if ntype == "heading":
        # ``attrs.level`` -> that many '#'. Missing/garbage level degrades to a
        # level-1 heading rather than to a marker-less line, so the AC section
        # is still recognizable. The text is stripped because the miner's
        # pattern is end-anchored ('...criteria$').
        try:
            raw_level = int((node.get("attrs") or {}).get("level"))
        except (TypeError, ValueError):
            raw_level = 1
        level = min(max(raw_level, 1), _MAX_HEADING_LEVEL)
        text = inner.strip()
        return "#" * level + (f" {text}" if text else "") + "\n"
    if ntype == "paragraph":
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
            owner_names=(
                [str(assignee.get("displayName"))]
                if assignee.get("displayName")
                else []
            ),
            app_url=browse,
            created_at=parse_iso8601(fields.get("created")),
            comments=comments,
            attachments=attachments,
            provider="jira",
            slug=self.make_slug(key),
            source_label=self.label,
            # Status name, spelled exactly as list_states() spells it — the
            # assigned-tickets panel matches the two to order its buckets and to
            # tell whether the issue sits in the source's ingest state.
            state=str((fields.get("status") or {}).get("name") or ""),
        )

    async def _search(self, jql: str) -> list[Ticket]:
        """One enhanced-search call for ``jql`` -> tickets. Single source of the
        endpoint, field set, accepted statuses and error format for both the
        pipeline poll and the unfiltered panel listing."""
        body = {
            "jql": jql,
            "maxResults": _MAX_ISSUES,
            "fields": list(_ISSUE_FIELDS),
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

    def _state_clause(self) -> str:
        """The source's ingest-state filter as a JQL ``AND status IN (…)``
        clause; empty when the source ingests from any status."""
        states = workflow_state_list(self.cfg)
        if not states:
            return ""
        # Numeric = a status id; otherwise a status name (quoted for JQL).
        # Several configured ingest states become one IN clause.
        quoted = ", ".join(s if s.isdigit() else f'"{s}"' for s in states)
        return f" AND status IN ({quoted})"

    def _assignee_clause(self) -> str:
        """The JQL assignee scope, with its trailing ``AND``.

        Empty under ``assignee_scope = "anyone"``: a QA queue takes whatever sits
        in the ingest status, whoever it belongs to. ``ingests_any_assignee``
        guarantees a status filter is configured before that happens, so the
        search is never unbounded."""
        return "" if ingests_any_assignee(self.cfg) else "assignee = currentUser() AND "

    async def search_assigned(self, since: datetime) -> list[Ticket]:
        jql = (
            f'{self._assignee_clause()}updated >= "{since.strftime("%Y-%m-%d %H:%M")}"'
            f"{self._state_clause()} ORDER BY updated DESC"
        )
        return await self._search(jql)

    async def search_assigned_all(self) -> list[Ticket]:
        """Every issue currently assigned to the user, in ANY status and with no
        ``updated`` cutoff: the source's ``workflow_state`` ingest filter is
        deliberately omitted so the panel can list — and force-start — the issue
        you are about to move INTO that status, which is precisely the case the
        panel exists for. ``Ticket.state`` carries the status name (the bucket).

        The one exception is an any-assignee source, where the status filter is
        the only thing standing between the panel and every issue on the site —
        there it stays applied."""
        if ingests_any_assignee(self.cfg):
            clause = self._state_clause().removeprefix(" AND ")
            return await self._search(f"{clause} ORDER BY updated DESC")
        return await self._search("assignee = currentUser() ORDER BY updated DESC")

    async def fetch(self, ticket_id: str) -> Ticket:
        params = {"fields": ",".join(_ISSUE_FIELDS)}
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
        is the numeric status id used in the JQL ``status = <id>`` filter;
        ``type`` is the status' category translated into the shared
        ``unstarted``/``started``/``done`` vocabulary."""
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
            category = str((s.get("statusCategory") or {}).get("key") or "").lower()
            out.append(
                {
                    "id": sid,
                    "name": name,
                    # unstarted | started | done — the same key and vocabulary
                    # the Shortcut adapter emits, so the assigned-tickets panel
                    # can park done-type buckets behind the Add menu. "" when
                    # Jira reports no category (bucket stays unparked).
                    "type": _STATUS_CATEGORY_TYPES.get(category, ""),
                }
            )
        return out
