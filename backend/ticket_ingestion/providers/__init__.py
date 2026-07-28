"""Ticket-provider registry.

Maps a provider name to its adapter class and its UI credential schema. The
pipeline calls :func:`get_provider`; the web layer reads :data:`PROVIDER_META`
to render the right fields and drive the per-provider "Test connection" flow.
"""

from __future__ import annotations

from typing import Type

from backend.ticket_ingestion.config import TicketProviderConfig
from backend.ticket_ingestion.providers.asana import AsanaProvider
from backend.ticket_ingestion.providers.base import (
    ProviderError,
    TicketProvider,
    parse_acceptance_criteria,
)
from backend.ticket_ingestion.providers.github_issues import GithubIssuesProvider
from backend.ticket_ingestion.providers.jira import JiraProvider
from backend.ticket_ingestion.providers.linear import LinearProvider
from backend.ticket_ingestion.providers.shortcut import ShortcutProvider

__all__ = [
    "ProviderError",
    "TicketProvider",
    "parse_acceptance_criteria",
    "PROVIDER_REGISTRY",
    "PROVIDER_META",
    "get_provider",
    "provider_slug_prefix",
]

PROVIDER_REGISTRY: dict[str, Type[TicketProvider]] = {
    ShortcutProvider.name: ShortcutProvider,
    JiraProvider.name: JiraProvider,
    LinearProvider.name: LinearProvider,
    GithubIssuesProvider.name: GithubIssuesProvider,
    AsanaProvider.name: AsanaProvider,
}


# UI-facing description of each provider and the fields it needs. ``field.key``
# maps 1:1 onto a ``TicketProviderConfig`` attribute so the settings layer can
# store/resolve it generically. ``secret`` fields are masked on read.
PROVIDER_META: list[dict] = [
    {
        "id": "shortcut",
        "label": "Shortcut",
        "blurb": "Auto-create a session for each story assigned to you.",
        "fields": [
            {
                "key": "api_token",
                "label": "API token",
                "secret": True,
                "required": True,
                "placeholder": "Shortcut API token",
            },
            {
                "key": "member_id",
                "label": "Member ID",
                "secret": False,
                "required": True,
                "placeholder": "auto-filled by Test",
                "auto": True,
            },
            {
                "key": "workflow_state",
                "label": "Ingest states",
                "type": "state",
                "secret": False,
                "required": False,
                "placeholder": "any state (add one or more)",
            },
        ],
    },
    {
        "id": "jira",
        "label": "Jira",
        "blurb": "Ingest Jira issues assigned to you (assignee = currentUser()).",
        "fields": [
            {
                "key": "base_url",
                "label": "Site URL",
                "secret": False,
                "required": True,
                "placeholder": "https://your-domain.atlassian.net",
            },
            {
                "key": "email",
                "label": "Account email",
                "secret": False,
                "required": True,
                "placeholder": "you@company.com",
            },
            {
                "key": "api_token",
                "label": "API token",
                "secret": True,
                "required": True,
                "placeholder": "Atlassian API token",
            },
            {
                "key": "member_id",
                "label": "Account ID",
                "secret": False,
                "required": False,
                "placeholder": "auto-filled by Test (optional)",
                "auto": True,
            },
            {
                "key": "workflow_state",
                "label": "Ingest statuses",
                "type": "state",
                "secret": False,
                "required": False,
                "placeholder": "any status (add one or more)",
            },
        ],
    },
    {
        "id": "linear",
        "label": "Linear",
        "blurb": "Ingest Linear issues assigned to you.",
        "fields": [
            {
                "key": "api_token",
                "label": "API key",
                "secret": True,
                "required": True,
                "placeholder": "lin_api_…",
            },
            {
                "key": "member_id",
                "label": "User ID",
                "secret": False,
                "required": False,
                "placeholder": "auto-filled by Test (optional)",
                "auto": True,
            },
            {
                "key": "workflow_state",
                "label": "Ingest states",
                "type": "state",
                "secret": False,
                "required": False,
                "placeholder": "any state (add one or more)",
            },
        ],
    },
    {
        "id": "github_issues",
        "label": "GitHub Issues",
        "blurb": "Ingest GitHub issues assigned to you in a repo. Reuses your GitHub connection.",
        "fields": [
            {
                "key": "project",
                "label": "Repository",
                "secret": False,
                "required": True,
                "placeholder": "owner/repo",
            },
            {
                "key": "api_token",
                "label": "Token",
                "secret": True,
                "required": False,
                "placeholder": "optional — falls back to your GitHub connection",
            },
        ],
    },
    {
        "id": "asana",
        "label": "Asana",
        "blurb": "Ingest Asana tasks assigned to you in a workspace.",
        "fields": [
            {
                "key": "api_token",
                "label": "Access token",
                "secret": True,
                "required": True,
                "placeholder": "Asana personal access token",
            },
            {
                "key": "project",
                "label": "Workspace GID",
                "secret": False,
                "required": True,
                "placeholder": "1201234567890",
            },
        ],
    },
]


def get_provider(cfg: TicketProviderConfig) -> TicketProvider:
    """Instantiate the adapter for ``cfg.provider``."""
    cls = PROVIDER_REGISTRY.get((cfg.provider or "shortcut").strip().lower())
    if cls is None:
        raise ProviderError(
            f"Unknown ticketing provider {cfg.provider!r}; "
            f"expected one of {', '.join(PROVIDER_REGISTRY)}"
        )
    return cls(cfg)


def provider_slug_prefix(provider: str) -> str:
    """The default slug/branch prefix for a provider name (``sc``/``jira``/…),
    without instantiating the adapter. Falls back to a sanitized provider name
    for unknown providers."""
    cls = PROVIDER_REGISTRY.get((provider or "shortcut").strip().lower())
    if cls is not None:
        return cls.slug_prefix
    return (provider or "src").strip().lower() or "src"
