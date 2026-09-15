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
import re
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


_ADF_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_ADF_BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")


def text_to_adf(text: str) -> list[dict]:
    """The inverse of :func:`flatten_adf`, to the depth the merge path needs.

    Jira is the one supported tracker whose description is not markdown, so the
    merged-in section — which is assembled once, in markdown, for every provider
    — has to be translated before it can be written back. This understands
    exactly the four shapes :func:`backend.web.core.ticket_merge.merged_section`
    emits (``---`` rules, ``#`` headings, ``-`` bullets, plain paragraphs) and
    degrades anything else to a paragraph, which is lossless for text even when
    it loses formatting.

    Deliberately NOT a general markdown-to-ADF converter: this is only ever fed
    text this repo wrote, and a half-right converter guessing at tables and
    inline marks would fail on somebody's ticket rather than on a fixture.
    """
    nodes: list[dict] = []
    bullets: list[dict] = []

    def flush_bullets() -> None:
        if bullets:
            nodes.append({"type": "bulletList", "content": list(bullets)})
            bullets.clear()

    def paragraph(body: str) -> dict:
        # An empty paragraph has no `content` key at all — ADF rejects an empty
        # content array, which is what a naive [] would produce for a blank line.
        return (
            {"type": "paragraph", "content": [{"type": "text", "text": body}]}
            if body
            else {"type": "paragraph"}
        )

    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("---") and set(stripped) == {"-"}:
            flush_bullets()
            nodes.append({"type": "rule"})
            continue
        m = _ADF_HEADING.match(stripped)
        if m:
            flush_bullets()
            level = min(len(m.group(1)), _MAX_HEADING_LEVEL)
            body = m.group(2).strip()
            nodes.append(
                {
                    "type": "heading",
                    "attrs": {"level": level},
                    "content": [{"type": "text", "text": body}] if body else [],
                }
            )
            continue
        m = _ADF_BULLET.match(line)
        if m:
            bullets.append(
                {"type": "listItem", "content": [paragraph(m.group(1).strip())]}
            )
            continue
        flush_bullets()
        if stripped:
            nodes.append(paragraph(stripped))
    flush_bullets()
    return nodes


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

    async def set_state(self, ticket_id: str, state_id: str) -> None:
        """Move an issue to status ``state_id`` by executing its transition.

        Jira statuses are not writable directly: an issue moves along the
        transitions its workflow offers from where it currently sits. So this
        asks the issue which transitions it has (``GET …/transitions``), picks
        the one whose destination is the configured status — by id, and by name
        as the fallback, since :meth:`list_states` stores ids but a hand-edited
        config may hold a name — and executes it.

        A status that is real but not reachable from the issue's current one is
        the common failure, and it is a configuration answer rather than a bug,
        so the error names the transitions that WERE on offer.
        """
        target = str(state_id).strip()
        if not target:
            return
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(
                self._api(f"/rest/api/3/issue/{ticket_id}/transitions"),
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        f"Jira could not list transitions for {ticket_id} "
                        f"(HTTP {resp.status}): {text[:200]}"
                    )
                data = await resp.json()
            transitions = data.get("transitions") or []
            match = None
            offered: list[str] = []
            for t in transitions:
                to = t.get("to") or {}
                name = str(to.get("name") or "")
                offered.append(name or str(t.get("name") or ""))
                if str(to.get("id") or "") == target or name == target:
                    match = t
                    break
            if match is None:
                # Already there is not a failure: an issue sitting in the target
                # status simply has no transition INTO it.
                raise ProviderError(
                    f"Jira has no transition from issue {ticket_id}'s current "
                    f"status to {target!r}"
                    + (f" (offered: {', '.join(offered)})" if offered else "")
                )
            async with session.post(
                self._api(f"/rest/api/3/issue/{ticket_id}/transitions"),
                json={"transition": {"id": str(match.get("id"))}},
                headers=self._headers(),
            ) as resp:
                if resp.status not in (200, 201, 204):
                    text = await resp.text()
                    raise ProviderError(
                        f"Jira refused to move issue {ticket_id} to {target!r} "
                        f"(HTTP {resp.status}): {text[:200]}"
                    )

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

    # ----------------------------------------------------------------- #
    # Writes (Intake → Tickets → Merge into…). See TicketProvider for the
    # contract and for why the order in ticket_merge.py is the order it is.
    # ----------------------------------------------------------------- #
    can_merge = True

    async def append_description(self, ticket_id: str, addition: str) -> None:
        """Append to the issue description, translating markdown to ADF.

        Read-modify-write on the raw ADF rather than on the flattened text
        :meth:`fetch` returns: writing back a flattened description would strip
        every table, panel, code block and inline mark the issue already had —
        a merge is not a licence to reformat somebody else's ticket.
        """
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(
                self._api(f"/rest/api/3/issue/{ticket_id}"),
                params={"fields": "description"},
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        f"Jira API returned {resp.status} for issue "
                        f"{ticket_id}: {text[:200]}"
                    )
                data = await resp.json()
        current = (data.get("fields") or {}).get("description")
        content = list((current or {}).get("content") or [])
        doc = {
            "type": "doc",
            "version": int((current or {}).get("version") or 1),
            "content": content + text_to_adf(addition),
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.put(
                self._api(f"/rest/api/3/issue/{ticket_id}"),
                json={"fields": {"description": doc}},
                headers=self._headers(),
            ) as resp:
                if resp.status not in (200, 201, 204):
                    text = await resp.text()
                    raise ProviderError(
                        f"Jira could not update issue {ticket_id} "
                        f"(HTTP {resp.status}): {text[:200]}"
                    )

    async def add_comment(self, ticket_id: str, body: str) -> None:
        doc = {"type": "doc", "version": 1, "content": text_to_adf(body)}
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                self._api(f"/rest/api/3/issue/{ticket_id}/comment"),
                json={"body": doc},
                headers=self._headers(),
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise ProviderError(
                        f"Jira could not comment on issue {ticket_id} "
                        f"(HTTP {resp.status}): {text[:200]}"
                    )

    async def carry_attachments(
        self, from_id: str, to_id: str
    ) -> tuple[list[str], list[str]]:
        """Download each of the source issue's attachments and re-upload them
        onto the target.

        The one provider here where the bytes genuinely have to move: a Jira
        attachment belongs to its issue and is destroyed with it, so a link
        copied into the description would 404 the moment the source is deleted.

        Per-file best effort. One oversized or expired attachment comes back in
        ``failed`` and the rest still land — losing four files because the fifth
        was unreadable would be a worse answer than losing the fifth.
        """
        issue = await self.fetch(from_id)
        moved: list[str] = []
        failed: list[str] = []
        upload_headers = {
            "Authorization": self._headers()["Authorization"],
            # Jira's XSRF check rejects a multipart POST without it.
            "X-Atlassian-Token": "no-check",
            "Accept": "application/json",
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            for att in issue.attachments:
                try:
                    async with session.get(
                        att.url, headers=att.auth_headers or {}
                    ) as resp:
                        if resp.status != 200:
                            failed.append(att.name)
                            continue
                        blob = await resp.read()
                    form = aiohttp.FormData()
                    form.add_field(
                        "file",
                        blob,
                        filename=att.name,
                        content_type=att.content_type or "application/octet-stream",
                    )
                    async with session.post(
                        self._api(f"/rest/api/3/issue/{to_id}/attachments"),
                        data=form,
                        headers=upload_headers,
                    ) as resp:
                        if resp.status not in (200, 201):
                            failed.append(att.name)
                            continue
                    moved.append(att.name)
                except (aiohttp.ClientError, OSError) as err:  # noqa: PERF203
                    _logger.warning(
                        "Could not carry Jira attachment %s from %s to %s: %s",
                        att.name,
                        from_id,
                        to_id,
                        err,
                    )
                    failed.append(att.name)
        return moved, failed

    async def delete_ticket(self, ticket_id: str) -> None:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.delete(
                self._api(f"/rest/api/3/issue/{ticket_id}"),
                # Subtasks cannot outlive their parent — Jira refuses the delete
                # outright (400) unless this says what to do with them.
                params={"deleteSubtasks": "true"},
                headers=self._headers(),
            ) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    hint = (
                        " — your Jira account needs the project's Delete Issues "
                        "permission"
                        if resp.status == 403
                        else ""
                    )
                    raise ProviderError(
                        f"Jira refused to delete issue {ticket_id} "
                        f"(HTTP {resp.status}){hint}: {text[:200]}"
                    )
