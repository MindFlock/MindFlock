"""Ticket-provider interface + source-agnostic parsing helpers.

A :class:`TicketProvider` is the seam that lets the ingestion pipeline pull work
items from any ticketing system. Each adapter turns that system's API into the
pipeline's normalized :class:`~backend.ticket_ingestion.models.Ticket`, and
the rest of the pipeline (validator, prompt builder, provisioner, runners) is
provider-agnostic.

Two responsibilities per adapter:

* :meth:`TicketProvider.search_assigned` — poll for tickets assigned to the
  configured user that changed since a timestamp (the provider does the
  "assigned to me" filtering server-side where it can).
* :meth:`TicketProvider.fetch` — full detail for one ticket by native id (used
  by the webhook path and to hydrate slim search results).

The markdown-shaped helpers here (acceptance-criteria mining, link/attachment
extraction) are shared: every provider that hands us markdown/plain-text
descriptions reuses the exact same rules the Shortcut pipeline always used.
"""

from __future__ import annotations

import abc
import re
from datetime import datetime, timezone
from typing import Iterable

import aiohttp

from backend.ticket_ingestion.config import TicketProviderConfig
from backend.ticket_ingestion.models import Attachment, Ticket

# Total wall-clock budget for any single provider HTTP request/response.
# Centralized so the per-source request budget is one policy the adapters share
# rather than five identical copies.
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)


class ProviderError(RuntimeError):
    """Raised for an unrecoverable provider API/auth failure.

    Subclasses :class:`RuntimeError` so it stays compatible with the pipeline's
    long-standing "fetch failed -> RuntimeError" contract."""


# --------------------------------------------------------------------------- #
# Shared, source-agnostic parsing (identical rules to the original Shortcut
# pipeline, now reusable by every provider).
# --------------------------------------------------------------------------- #
def parse_acceptance_criteria(description: str) -> list[str]:
    """Mine acceptance criteria from a markdown description.

    Looks for an ``## Acceptance Criteria`` section (bullets / numbered items),
    falls back to any top-level bullets when there is no such section, and also
    collects Gherkin-style ``WHEN/THEN/AND`` blocks. De-duplicated, order-preserving.
    """
    if not description:
        return []
    lines = description.splitlines()
    criteria: list[str] = []

    ac_header_pattern = re.compile(
        r"^\s*#+\s*(acceptance criteria|acceptance criterion)\s*$", re.IGNORECASE
    )
    other_header_pattern = re.compile(r"^\s*#+\s+\S")
    bullet_pattern = re.compile(r"^\s*[-*]\s+(.*\S)")
    numbered_pattern = re.compile(r"^\s*\d+\.\s+(.*\S)")

    in_ac_section = False
    found_ac_section = False
    for line in lines:
        if ac_header_pattern.match(line):
            in_ac_section = True
            found_ac_section = True
            continue
        if in_ac_section:
            if other_header_pattern.match(line):
                in_ac_section = False
                continue
            m = bullet_pattern.match(line) or numbered_pattern.match(line)
            if m:
                criteria.append(m.group(1).strip())

    if not found_ac_section:
        for line in lines:
            m = bullet_pattern.match(line) or numbered_pattern.match(line)
            if m:
                criteria.append(m.group(1).strip())

    in_when = False
    when_block: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^WHEN\b", stripped, re.IGNORECASE):
            if when_block:
                criteria.append(" ".join(when_block).strip())
                when_block = []
            in_when = True
            when_block.append(stripped)
        elif in_when and re.match(r"^(THEN|AND)\b", stripped, re.IGNORECASE):
            when_block.append(stripped)
        elif in_when and stripped == "":
            if when_block:
                criteria.append(" ".join(when_block).strip())
                when_block = []
            in_when = False
        elif in_when:
            when_block.append(stripped)
    if when_block:
        criteria.append(" ".join(when_block).strip())

    seen: set[str] = set()
    unique: list[str] = []
    for c in criteria:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


_URL_PATTERN = re.compile(r"https?://[^\s)<>\]\"']+", re.IGNORECASE)

# Extensions clearly worth pulling into the prompt as files.
_FILE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".log",
    ".html",
    ".xml",
}


def has_file_extension(url: str) -> bool:
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return any(path.endswith(ext) for ext in _FILE_EXTENSIONS)


def extract_link_attachments(
    text_blobs: Iterable[str],
    *,
    seen_urls: set[str] | None = None,
    is_hosted=lambda url: False,
    hosted_headers=lambda url: {},
) -> list[Attachment]:
    """Collect file-ish links from free text (description + comments).

    Includes any URL the provider considers "hosted" (``is_hosted``) plus any
    link ending in a known file extension. ``hosted_headers`` supplies the auth
    headers needed to download a hosted URL.
    """
    attachments: list[Attachment] = []
    seen = seen_urls if seen_urls is not None else set()
    for blob in text_blobs:
        if not blob:
            continue
        for raw_url in _URL_PATTERN.findall(blob):
            url = raw_url.rstrip(".,;:!?")
            if not url or url in seen:
                continue
            hosted = is_hosted(url)
            if not (hosted or has_file_extension(url)):
                continue
            seen.add(url)
            name = url.split("?", 1)[0].rsplit("/", 1)[-1] or "attachment"
            attachments.append(
                Attachment(
                    name=name,
                    url=url,
                    content_type=None,
                    auth_headers=dict(hosted_headers(url)) if hosted else {},
                )
            )
    return attachments


def parse_iso8601(raw: str | None) -> datetime:
    """Parse an ISO-8601 timestamp (tolerant of a trailing ``Z``); UTC epoch on
    failure/empty."""
    if not raw:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def workflow_state_list(cfg: TicketProviderConfig) -> list[str]:
    """The source's ingest-state filter as a list of state ids.

    ``cfg.workflow_state`` holds one id or several comma-separated ids (the
    Settings picker writes the joined form); a legacy single id and an empty
    filter both work. Order-preserving, blanks dropped."""
    raw = (getattr(cfg, "workflow_state", "") or "").strip()
    return [s for s in (x.strip() for x in raw.split(",")) if s]


# --------------------------------------------------------------------------- #
# Provider interface
# --------------------------------------------------------------------------- #
class TicketProvider(abc.ABC):
    """Adapter turning one ticketing system into normalized :class:`Ticket`s."""

    #: Machine id, matches ``[ticketing].provider`` (e.g. ``"shortcut"``).
    name: str = ""
    #: Human label used in prompts ("Shortcut URL:", "Jira URL:").
    label: str = ""
    #: Branch/session slug prefix (``feature/<prefix>-<id>/<name>``).
    slug_prefix: str = ""

    def __init__(self, cfg: TicketProviderConfig) -> None:
        self.cfg = cfg
        # A per-source id (config ``id``) overrides the class default so several
        # sources — including two of the same provider — get distinct,
        # non-colliding slugs/branches. Empty id => the historic class prefix.
        if getattr(cfg, "id", ""):
            self.slug_prefix = cfg.id
        # Instance label: an explicit source label, else the class default.
        if getattr(cfg, "label", ""):
            self.label = cfg.label

    def make_slug(self, native_id: object) -> str:
        """Provider-scoped, filesystem/branch-safe handle for a ticket.

        Globally unique across providers so workspaces, branches and dedup keys
        never collide. Non-alnum chars in the id collapse to ``-``.
        """
        token = re.sub(r"[^A-Za-z0-9]+", "-", str(native_id)).strip("-") or "0"
        return f"{self.slug_prefix}-{token}"

    @abc.abstractmethod
    async def search_assigned(self, since: datetime) -> list[Ticket]:
        """Tickets assigned to the configured user, changed since ``since``."""

    async def search_assigned_all(self) -> list[Ticket]:
        """EVERY ticket currently assigned to the configured user — no age
        cutoff and, where the adapter supports it, no workflow-state filter —
        with :attr:`Ticket.state` set to the bucket name when known.

        Backs the web UI's assigned-tickets panel (grouped by bucket), not
        the pipeline. Default: an epoch-anchored :meth:`search_assigned`
        (which may still apply the source's workflow-state filter);
        providers override to drop that filter and annotate the state.
        """
        return await self.search_assigned(datetime(1970, 1, 1, tzinfo=timezone.utc))

    @abc.abstractmethod
    async def fetch(self, ticket_id: str) -> Ticket:
        """Full detail for one ticket by its native id."""

    async def test_connection(self) -> tuple[dict | None, str]:
        """Validate credentials. Returns ``(identity_dict, "")`` on success or
        ``(None, error_message)`` on failure. ``identity_dict`` may carry a
        resolved ``member_id`` so the UI can auto-fill it.

        Default: perform a one-item search and treat any non-error as success.
        Providers override to hit a cheaper identity endpoint.
        """
        try:
            await self.search_assigned(datetime.now(timezone.utc))
        except ProviderError as e:
            return None, str(e)
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"
        return {}, ""

    async def list_states(self) -> list[dict]:
        """The workflow states/statuses a ticket can be in, for the "ingest only
        when the ticket is in state X" picker. Each entry is ``{"id": str,
        "name": str}`` (``id`` is what gets stored in ``cfg.workflow_state``).

        Default: ``[]`` — providers without workflow states (GitHub Issues,
        Asana) don't offer the picker. Shortcut/Jira/Linear override.
        """
        return []
