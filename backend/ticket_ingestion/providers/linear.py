"""Linear provider (GraphQL).

Queries ``viewer.assignedIssues`` filtered by ``updatedAt >= since`` against
``https://api.linear.app/graphql``. Descriptions are markdown, so the shared
acceptance-criteria miner works directly. Auth: a personal API key passed in the
``Authorization`` header verbatim (no ``Bearer`` prefix), per Linear's docs.
"""

from __future__ import annotations

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
_ENDPOINT = "https://api.linear.app/graphql"
_MAX_ISSUES = 50

_ISSUE_FIELDS = """
  id
  identifier
  title
  description
  url
  createdAt
  assignee { id name }
  comments(first: 50) { nodes { body user { name } createdAt } }
  attachments(first: 50) { nodes { url title } }
"""


def _search_query(with_state: bool) -> str:
    """The assigned-issues query, optionally gated to workflow state(s)."""
    params = "$since: DateTimeOrDuration!, $n: Int!"
    filt = "updatedAt: { gte: $since }"
    if with_state:
        params += ", $stateIds: [ID!]!"
        filt += ", state: { id: { in: $stateIds } }"
    return (
        f"query({params}) {{"
        f" viewer {{ assignedIssues("
        f"   first: $n, filter: {{ {filt} }},"
        f"   orderBy: updatedAt"
        f" ) {{ nodes {{{_ISSUE_FIELDS}}} }} }} }}"
    )


_FETCH_QUERY = "query($id: String!) { issue(id: $id) {" + _ISSUE_FIELDS + "} }"
_VIEWER_QUERY = "query { viewer { id name email } }"
_STATES_QUERY = (
    "query { workflowStates(first: 250) { nodes { id name type team { key } } } }"
)


class LinearProvider(TicketProvider):
    name = "linear"
    label = "Linear"
    slug_prefix = "lin"

    def _headers(self) -> dict[str, str]:
        # Linear personal API keys go in Authorization verbatim (no "Bearer ").
        return {"Authorization": self.cfg.api_token, "Content-Type": "application/json"}

    async def _gql(self, query: str, variables: dict) -> dict:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                _ENDPOINT,
                json={"query": query, "variables": variables},
                headers=self._headers(),
            ) as resp:
                if resp.status == 401:
                    raise ProviderError("Linear rejected the API key (HTTP 401)")
                if resp.status != 200:
                    text = await resp.text()
                    raise aiohttp.ClientError(
                        f"Linear API returned {resp.status}: {text[:200]}"
                    )
                payload = await resp.json()
        if payload.get("errors"):
            raise ProviderError(f"Linear GraphQL error: {payload['errors']}")
        return payload.get("data") or {}

    def _issue_to_ticket(self, issue: dict[str, Any]) -> Ticket:
        identifier = str(issue.get("identifier") or issue.get("id") or "")
        description = issue.get("description") or ""

        comments = []
        for c in (issue.get("comments") or {}).get("nodes") or []:
            body = (c.get("body") or "").strip()
            if not body:
                continue
            author = (c.get("user") or {}).get("name") or "unknown"
            comments.append(f"[{c.get('createdAt') or ''} by {author}] {body}")

        attachments: list[Attachment] = []
        for a in (issue.get("attachments") or {}).get("nodes") or []:
            url = a.get("url")
            if not url:
                continue
            attachments.append(Attachment(name=a.get("title") or "attachment", url=url))

        assignee = issue.get("assignee") or {}
        return Ticket(
            id=identifier,
            name=str(issue.get("title") or ""),
            description=description,
            acceptance_criteria=parse_acceptance_criteria(description),
            owner_ids=[str(assignee.get("id"))] if assignee.get("id") else [],
            app_url=issue.get("url") or "",
            created_at=parse_iso8601(issue.get("createdAt")),
            comments=comments,
            attachments=attachments,
            provider="linear",
            slug=self.make_slug(identifier),
            source_label=self.label,
        )

    async def search_assigned(self, since: datetime) -> list[Ticket]:
        states = workflow_state_list(self.cfg)
        variables: dict = {"since": since.isoformat(), "n": _MAX_ISSUES}
        if states:
            variables["stateIds"] = states
        data = await self._gql(_search_query(bool(states)), variables)
        nodes = (
            ((data.get("viewer") or {}).get("assignedIssues") or {}).get("nodes")
        ) or []
        return [self._issue_to_ticket(n) for n in nodes]

    async def fetch(self, ticket_id: str) -> Ticket:
        data = await self._gql(_FETCH_QUERY, {"id": ticket_id})
        issue = data.get("issue")
        if not issue:
            raise ProviderError(f"Linear issue {ticket_id} not found")
        return self._issue_to_ticket(issue)

    async def test_connection(self) -> tuple[dict | None, str]:
        try:
            data = await self._gql(_VIEWER_QUERY, {})
        except ProviderError as e:
            return None, str(e)
        except aiohttp.ClientError as e:
            return None, f"network error reaching Linear: {e}"
        viewer = data.get("viewer") or {}
        return {"member_id": str(viewer.get("id", "")), "name": viewer.get("name")}, ""

    async def list_states(self) -> list[dict]:
        """All Linear workflow states across teams. The team key is prefixed onto
        the name so states from different teams stay distinguishable; the stored
        id is the state's globally-unique id used in the search filter."""
        data = await self._gql(_STATES_QUERY, {})
        nodes = ((data.get("workflowStates") or {}).get("nodes")) or []
        out: list[dict] = []
        for n in nodes:
            sid = str(n.get("id") or "")
            if not sid:
                continue
            name = n.get("name") or sid
            team = (n.get("team") or {}).get("key") or ""
            out.append({"id": sid, "name": f"{team} · {name}" if team else name})
        return out
