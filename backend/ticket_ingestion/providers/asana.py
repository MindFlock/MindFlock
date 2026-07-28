"""Asana provider.

Ingests tasks assigned to you (``assignee=me``) within a workspace
(``project`` = workspace gid), changed since the last run. Auth: a personal
access token (Bearer). Task notes are plain text; comments come from the task's
"stories" (type=comment) and attachments from the attachments endpoint.
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
)

_logger = logging.getLogger(__name__)
# Shared request budget (defined once in providers/base.py).
_HTTP_TIMEOUT = HTTP_TIMEOUT
_API = "https://app.asana.com/api/1.0"
_MAX_TASKS = 50


class AsanaProvider(TicketProvider):
    name = "asana"
    label = "Asana"
    slug_prefix = "asana"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_token}",
            "Accept": "application/json",
        }

    async def _get(
        self, session: aiohttp.ClientSession, path: str, params: dict | None = None
    ) -> Any:
        async with session.get(
            f"{_API}{path}", params=params or {}, headers=self._headers()
        ) as resp:
            if resp.status in (401, 403):
                raise ProviderError(f"Asana rejected the token (HTTP {resp.status})")
            if resp.status != 200:
                text = await resp.text()
                raise aiohttp.ClientError(
                    f"Asana API returned {resp.status}: {text[:200]}"
                )
            payload = await resp.json()
        return payload.get("data")

    async def _comments(self, session: aiohttp.ClientSession, gid: str) -> list[str]:
        try:
            stories = await self._get(
                session,
                f"/tasks/{gid}/stories",
                {"opt_fields": "type,text,created_at,created_by.name"},
            )
        except (ProviderError, aiohttp.ClientError):
            return []
        out = []
        for s in stories or []:
            if s.get("type") != "comment":
                continue
            text = (s.get("text") or "").strip()
            if not text:
                continue
            author = (s.get("created_by") or {}).get("name") or "unknown"
            out.append(f"[{s.get('created_at') or ''} by {author}] {text}")
        return out

    async def _attachments(
        self, session: aiohttp.ClientSession, gid: str
    ) -> list[Attachment]:
        try:
            atts = await self._get(
                session,
                "/attachments",
                {"parent": gid, "opt_fields": "name,download_url,view_url"},
            )
        except (ProviderError, aiohttp.ClientError):
            return []
        out = []
        for a in atts or []:
            url = a.get("download_url") or a.get("view_url")
            if not url:
                continue
            out.append(Attachment(name=a.get("name") or "attachment", url=url))
        return out

    async def _task_to_ticket(
        self, session: aiohttp.ClientSession, task: dict[str, Any]
    ) -> Ticket:
        gid = str(task.get("gid") or "")
        notes = task.get("notes") or ""
        assignee = task.get("assignee") or {}
        return Ticket(
            id=gid,
            name=str(task.get("name") or ""),
            description=notes,
            acceptance_criteria=parse_acceptance_criteria(notes),
            owner_ids=[str(assignee.get("gid"))] if assignee.get("gid") else [],
            app_url=task.get("permalink_url") or "",
            created_at=parse_iso8601(task.get("created_at")),
            comments=await self._comments(session, gid),
            attachments=await self._attachments(session, gid),
            provider="asana",
            slug=self.make_slug(gid),
            source_label=self.label,
        )

    async def search_assigned(self, since: datetime) -> list[Ticket]:
        if not self.cfg.project:
            raise ProviderError("asana requires ticketing.project = the workspace gid")
        params = {
            "assignee": self.cfg.member_id or "me",
            "workspace": self.cfg.project,
            "modified_since": since.isoformat(),
            "completed_since": "now",  # exclude tasks completed before now
            "limit": str(_MAX_TASKS),
            "opt_fields": "name,notes,permalink_url,assignee.gid,created_at,completed",
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            tasks = await self._get(session, "/tasks", params)
            out = []
            for t in tasks or []:
                if t.get("completed"):
                    continue
                out.append(await self._task_to_ticket(session, t))
        return out

    async def fetch(self, ticket_id: str) -> Ticket:
        params = {
            "opt_fields": "name,notes,permalink_url,assignee.gid,created_at,completed"
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            task = await self._get(session, f"/tasks/{ticket_id}", params)
            if not task:
                raise ProviderError(f"Asana task {ticket_id} not found")
            return await self._task_to_ticket(session, task)

    async def test_connection(self) -> tuple[dict | None, str]:
        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                me = await self._get(session, "/users/me", {"opt_fields": "name,gid"})
        except ProviderError as e:
            return None, str(e)
        except aiohttp.ClientError as e:
            return None, f"network error reaching Asana: {e}"
        return {
            "member_id": str((me or {}).get("gid", "")),
            "name": (me or {}).get("name"),
        }, ""
