"""Shortcut provider — the original ingestion source, now behind the interface.

Wraps Shortcut's REST API (``/stories/search`` + per-story hydration) and its
attachment auth (``Shortcut-Token`` header for Shortcut-hosted files). The
parsing helpers here are the exact rules the pipeline always used, so existing
behaviour and tests are preserved byte-for-byte.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import aiohttp

from backend.ticket_ingestion.models import Attachment, Ticket
from backend.ticket_ingestion.providers.base import (
    HTTP_TIMEOUT,
    ProviderError,
    TicketProvider,
    extract_link_attachments,
    parse_acceptance_criteria,
    parse_iso8601,
    workflow_state_list,
)

_logger = logging.getLogger(__name__)

# Alias the shared request budget so the many _HTTP_TIMEOUT call sites here
# keep working; the value is defined once in providers/base.py.
_HTTP_TIMEOUT = HTTP_TIMEOUT
_SHORTCUT_API_BASE = "https://api.app.shortcut.com/api/v3"

# Per-story hydration retry (429/5xx/network are usually transient; a silent
# slim fallback loses the description the prompt is built from).
_HYDRATE_ATTEMPTS = 3
_HYDRATE_INITIAL_BACKOFF = 1.0
_HYDRATE_MAX_RETRY_AFTER = 30.0

# Shortcut-hosted file URLs need the Shortcut-Token header to download.
_SHORTCUT_HOSTS = (
    "api.app.shortcut.com",
    "files.shortcut.com",
    "app.shortcut.com",
)


def _is_shortcut_hosted(url: str) -> bool:
    return any(host in url for host in _SHORTCUT_HOSTS)


def _extract_comments(data: dict[str, Any]) -> list[str]:
    raw = data.get("comments") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        text = (c.get("text") or "").strip()
        if not text:
            continue
        author = c.get("author_id") or "unknown"
        created = c.get("created_at") or ""
        out.append(f"[{created} by {author}] {text}")
    return out


def _extract_attachments(data: dict[str, Any], token: str) -> list[Attachment]:
    """Attached files on the story plus file/image links in its text."""
    attachments: list[Attachment] = []
    seen_urls: set[str] = set()

    def hosted_headers(_url: str) -> dict[str, str]:
        return {"Shortcut-Token": token} if token else {}

    for raw_file in data.get("files") or []:
        if not isinstance(raw_file, dict):
            continue
        url = raw_file.get("url") or ""
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        hosted = _is_shortcut_hosted(url)
        attachments.append(
            Attachment(
                name=raw_file.get("name") or f"file-{raw_file.get('id', 'unknown')}",
                url=url,
                content_type=raw_file.get("content_type"),
                auth_headers=hosted_headers(url) if hosted else {},
            )
        )

    text_blobs = [data.get("description") or ""]
    for c in data.get("comments") or []:
        if isinstance(c, dict):
            text_blobs.append(c.get("text") or "")

    attachments.extend(
        extract_link_attachments(
            text_blobs,
            seen_urls=seen_urls,
            is_hosted=_is_shortcut_hosted,
            hosted_headers=hosted_headers,
        )
    )
    return attachments


def story_from_api_response(data: dict[str, Any], token: str = "") -> Ticket:
    """Build a :class:`Ticket` from a Shortcut story JSON object."""
    description = data.get("description") or ""
    story_id = int(data["id"])
    return Ticket(
        id=story_id,
        name=data.get("name", "") or "",
        description=description,
        acceptance_criteria=parse_acceptance_criteria(description),
        owner_ids=list(data.get("owner_ids", []) or []),
        app_url=data.get("app_url", "") or "",
        created_at=parse_iso8601(data.get("created_at") or "1970-01-01T00:00:00Z"),
        comments=_extract_comments(data),
        attachments=_extract_attachments(data, token),
        provider="shortcut",
        slug=f"sc-{story_id}",
        source_label="Shortcut",
    )


class ShortcutProvider(TicketProvider):
    name = "shortcut"
    label = "Shortcut"
    slug_prefix = "sc"

    def _headers(self) -> dict[str, str]:
        return {"Shortcut-Token": self.cfg.api_token}

    def _finalize(self, t: Ticket) -> Ticket:
        """Stamp the per-source slug prefix + label (supports multiple Shortcut
        sources with distinct ids)."""
        t.slug = self.make_slug(t.id)
        t.source_label = self.label
        return t

    async def _search_stories(self, body: dict) -> list:
        """One POST /stories/search call, returning the raw story list."""
        url = f"{_SHORTCUT_API_BASE}/stories/search"
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(url, json=body, headers=self._headers()) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise aiohttp.ClientError(
                        f"Shortcut API returned {resp.status}: {text[:200]}"
                    )
                data = await resp.json()
        return data if isinstance(data, list) else []

    def _ingest_state_ids(self) -> list[int]:
        """The configured ingest filter as Shortcut state ids. One or several
        comma-separated ids in ``workflow_state`` (the Settings picker writes
        the joined form); the typed integer field is the legacy fallback."""
        states = workflow_state_list(self.cfg)
        if not states and self.cfg.workflow_state_id is not None:
            states = [str(self.cfg.workflow_state_id)]
        out: list[int] = []
        for s in states:
            try:
                out.append(int(s))
            except ValueError:
                _logger.warning("Ignoring non-numeric Shortcut workflow_state %r", s)
        return out

    async def search_assigned(self, since: datetime) -> list[Ticket]:
        base_body: dict = {
            "owner_id": self.cfg.member_id,
            "updated_at_start": since.isoformat(),
        }
        # The search endpoint takes ONE workflow_state_id — several configured
        # ingest states mean one search per state, concatenated and de-duped.
        state_ids = self._ingest_state_ids()
        data: list = []
        seen_ids: set = set()
        for body in (
            [{**base_body, "workflow_state_id": sid} for sid in state_ids]
            if state_ids
            else [base_body]
        ):
            for item in await self._search_stories(body):
                sid = item.get("id") if isinstance(item, dict) else None
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                data.append(item)
        # /stories/search returns StorySlim (no description). Hydrate each by id.
        slim = [story_from_api_response(item, self.cfg.api_token) for item in data]
        full: list[Ticket] = []
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            for s in slim:
                full_data = await self._hydrate_story(session, s.id)
                if full_data is None:
                    # Last resort after retries: slim data (empty description).
                    full.append(self._finalize(s))
                    continue
                full.append(
                    self._finalize(
                        story_from_api_response(full_data, self.cfg.api_token)
                    )
                )
        return full

    async def search_assigned_all(self) -> list[Ticket]:
        """Every story currently assigned to the member, across ALL workflow
        states (the source's ``workflow_state`` ingest filter is deliberately
        not applied) and with no age cutoff. Slim data only — the panel needs
        id/name/url/state, and hydrating hundreds of stories one-by-one would
        hammer the API; the force-start path re-fetches the full story anyway.
        ``Ticket.state`` carries the workflow-state name (bucket)."""
        body: dict = {"owner_id": self.cfg.member_id}
        # Single source of the endpoint/accepted-status/error format (the guard
        # is redundant: _search_stories already returns [] for a non-list body).
        data = await self._search_stories(body)
        # Workflow-state id -> display name (bucket). Best-effort: an error
        # here degrades to unlabeled buckets, never an empty panel.
        try:
            state_names = {s["id"]: s["name"] for s in await self.list_states()}
        except Exception as err:  # noqa: BLE001
            _logger.warning("Could not resolve Shortcut workflow states: %s", err)
            state_names = {}
        out: list[Ticket] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            t = self._finalize(story_from_api_response(item, self.cfg.api_token))
            sid = item.get("workflow_state_id")
            t.state = state_names.get(str(sid), "") if sid is not None else ""
            out.append(t)
        return out

    async def _hydrate_story(
        self, session: aiohttp.ClientSession, story_id: int | str
    ) -> dict | None:
        """Fetch the full story, retrying transient failures (429/5xx/network)
        with short exponential backoff — honoring Retry-After when present —
        before letting the caller fall back to slim data with a warning."""
        detail_url = f"{_SHORTCUT_API_BASE}/stories/{story_id}"
        backoff = _HYDRATE_INITIAL_BACKOFF
        for attempt in range(1, _HYDRATE_ATTEMPTS + 1):
            status: int | str
            try:
                async with session.get(detail_url, headers=self._headers()) as resp:
                    status = resp.status
                    if resp.status == 200:
                        return await resp.json()
                    retryable = resp.status == 429 or resp.status >= 500
                    delay = backoff
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = min(float(retry_after), _HYDRATE_MAX_RETRY_AFTER)
                        except ValueError:
                            pass
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                status = f"error: {e}"
                retryable = True
                delay = backoff
            if not retryable or attempt == _HYDRATE_ATTEMPTS:
                break
            _logger.warning(
                "Fetching full story %s failed (%s); retrying in %.1fs "
                "(attempt %d/%d)",
                story_id,
                status,
                delay,
                attempt,
                _HYDRATE_ATTEMPTS,
            )
            await asyncio.sleep(delay)
            backoff *= 2
        _logger.warning(
            "Failed to fetch full story %s (%s) after %d attempt(s); using slim",
            story_id,
            status,
            attempt,
        )
        return None

    async def fetch(self, ticket_id: str) -> Ticket:
        url = f"{_SHORTCUT_API_BASE}/stories/{ticket_id}"
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        f"Shortcut API returned {resp.status} for story {ticket_id}: {text[:200]}"
                    )
                data = await resp.json()
        return self._finalize(story_from_api_response(data, self.cfg.api_token))

    async def test_connection(self) -> tuple[dict | None, str]:
        url = f"{_SHORTCUT_API_BASE}/member"
        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    if resp.status == 401:
                        return None, "Shortcut rejected the token (HTTP 401)"
                    if resp.status != 200:
                        return None, f"Shortcut API returned HTTP {resp.status}"
                    member = await resp.json()
        except aiohttp.ClientError as e:
            return None, f"network error reaching Shortcut: {e}"
        return {
            "member_id": str(member.get("id", "")),
            "name": member.get("mention_name"),
        }, ""

    async def list_states(self) -> list[dict]:
        """Every workflow state across all Shortcut workflows. When more than one
        workflow exists the state name is prefixed with the workflow name so
        duplicates (e.g. two "In Progress") stay distinguishable."""
        url = f"{_SHORTCUT_API_BASE}/workflows"
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        f"Shortcut /workflows returned HTTP {resp.status}: {text[:200]}"
                    )
                workflows = await resp.json()
        if not isinstance(workflows, list):
            return []
        multi = len(workflows) > 1
        out: list[dict] = []
        for wf in workflows:
            wf_name = wf.get("name") or ""
            for st in wf.get("states", []) or []:
                sid = st.get("id")
                if sid is None:
                    continue
                name = st.get("name") or str(sid)
                out.append(
                    {
                        "id": str(sid),
                        "name": f"{wf_name} · {name}" if multi else name,
                        # The qualifier and the bare state name, separately, so
                        # the panel can nest "Deferred" under "Product
                        # Development" instead of writing the workflow name onto
                        # all seven of its state headings. `name` stays the
                        # unique bucket key either way.
                        "group": wf_name if multi else "",
                        "label": name,
                        # unstarted | started | done — lets the assigned-tickets
                        # panel park done-type buckets behind the Add menu by
                        # default. Extra key; id/name consumers are unaffected.
                        "type": str(st.get("type") or ""),
                    }
                )
        return out
