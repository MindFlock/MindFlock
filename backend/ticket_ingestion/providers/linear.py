"""Linear provider (GraphQL).

Queries ``viewer.assignedIssues`` filtered by ``updatedAt >= since`` against
``https://api.linear.app/graphql`` — plus, for the UI's assigned-tickets panel,
the same query with no state filter and an epoch cutoff. Descriptions are
markdown, so the shared acceptance-criteria miner works directly. Auth: a
personal API key passed in the ``Authorization`` header verbatim (no ``Bearer``
prefix), per Linear's docs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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
_ENDPOINT = "https://api.linear.app/graphql"
_MAX_ISSUES = 50
# "No age cutoff" for the panel listing, expressed in the same
# ``updatedAt >= since`` shape the pipeline poll uses.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_ISSUE_FIELDS = """
  id
  identifier
  title
  description
  url
  createdAt
  assignee { id name }
  state { id name team { key } }
  comments(first: 50) { nodes { body user { name } createdAt } }
  attachments(first: 50) { nodes { url title } }
"""

# Linear's workflow-state ``type`` -> the vocabulary the assigned-tickets panel
# shares with the Shortcut adapter (``unstarted`` | ``started`` | ``done``).
# Linear spells its terminal states "completed"/"canceled", so passing its type
# through verbatim would leave the panel's done-parking (``type == "done"``)
# permanently false for Linear sources.
_STATE_TYPES = {
    "triage": "unstarted",
    "backlog": "unstarted",
    "unstarted": "unstarted",
    "started": "started",
    "completed": "done",
    "canceled": "done",
}


def _state_label(state: dict | None) -> str:
    """A workflow state's display name, spelled exactly as
    :meth:`LinearProvider.list_states` spells it (team-key prefixed).

    The assigned-tickets panel matches a ticket's :attr:`Ticket.state` against
    those names to order its buckets and to decide whether the ticket sits in
    the source's configured ingest state, so the two spellings must not drift.
    """
    if not state:
        return ""
    name = state.get("name") or str(state.get("id") or "")
    team = (state.get("team") or {}).get("key") or ""
    return f"{team} · {name}" if team else name


def _search_query(
    with_state: bool, *, any_assignee: bool = False, with_team: bool = False
) -> str:
    """The issue-search query, optionally gated to workflow state(s).

    Assignee scoping is structural in Linear: ``viewer.assignedIssues`` IS the
    "assigned to me" filter, so a source that ingests any assignee has to search
    from the top-level ``issues`` root instead, narrowed by state (and by team
    when the source names one)."""
    params = "$since: DateTimeOrDuration!, $n: Int!"
    filt = "updatedAt: { gte: $since }"
    if with_state:
        params += ", $stateIds: [ID!]!"
        filt += ", state: { id: { in: $stateIds } }"
    if any_assignee:
        if with_team:
            params += ", $team: String!"
            filt += ", team: { key: { eq: $team } }"
        return (
            f"query({params}) {{"
            f" issues("
            f"   first: $n, filter: {{ {filt} }},"
            f"   orderBy: updatedAt"
            f" ) {{ nodes {{{_ISSUE_FIELDS}}} }} }}"
        )
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
            owner_names=[str(assignee.get("name"))] if assignee.get("name") else [],
            app_url=issue.get("url") or "",
            created_at=parse_iso8601(issue.get("createdAt")),
            comments=comments,
            attachments=attachments,
            provider="linear",
            slug=self.make_slug(identifier),
            source_label=self.label,
            state=_state_label(issue.get("state")),
        )

    async def _assigned(self, since: datetime, *, with_state: bool) -> list[Ticket]:
        """Issues updated since ``since``, optionally narrowed to the source's
        ingest states. Single source of the query/variable shape for both the
        pipeline poll and the unfiltered panel listing — and of the assignee
        scope, which decides which GraphQL root the issues come from."""
        any_assignee = ingests_any_assignee(self.cfg)
        team = (self.cfg.project or "").strip() if any_assignee else ""
        variables: dict = {"since": since.isoformat(), "n": _MAX_ISSUES}
        if with_state:
            variables["stateIds"] = workflow_state_list(self.cfg)
        if team:
            variables["team"] = team
        data = await self._gql(
            _search_query(with_state, any_assignee=any_assignee, with_team=bool(team)),
            variables,
        )
        nodes = (
            ((data.get("issues") or {}).get("nodes") or [])
            if any_assignee
            else ((data.get("viewer") or {}).get("assignedIssues") or {}).get("nodes")
            or []
        )
        return [self._issue_to_ticket(n) for n in nodes]

    async def search_assigned(self, since: datetime) -> list[Ticket]:
        return await self._assigned(
            since, with_state=bool(workflow_state_list(self.cfg))
        )

    async def search_assigned_all(self) -> list[Ticket]:
        """Every issue currently assigned to the viewer, in ANY workflow state
        and with no age cutoff: the source's ``workflow_state`` ingest filter is
        deliberately omitted so the panel can list — and force-start — the issue
        you are about to move INTO that state, which is precisely the case the
        panel exists for. ``Ticket.state`` carries the state name (the bucket).

        An any-assignee source keeps the filter: it is the only bound on a search
        that no longer runs through ``viewer``."""
        return await self._assigned(_EPOCH, with_state=ingests_any_assignee(self.cfg))

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
        id is the state's globally-unique id used in the search filter; ``type``
        is Linear's state type translated into the shared
        ``unstarted``/``started``/``done`` vocabulary."""
        data = await self._gql(_STATES_QUERY, {})
        nodes = ((data.get("workflowStates") or {}).get("nodes")) or []
        out: list[dict] = []
        for n in nodes:
            sid = str(n.get("id") or "")
            if not sid:
                continue
            linear_type = str(n.get("type") or "").lower()
            out.append(
                {
                    "id": sid,
                    "name": _state_label(n),
                    # unstarted | started | done — the same key and vocabulary
                    # the Shortcut adapter emits, so the assigned-tickets panel
                    # can park done-type buckets behind the Add menu. "" for a
                    # state type Linear adds later (bucket stays unparked).
                    "type": _STATE_TYPES.get(linear_type, ""),
                }
            )
        return out
