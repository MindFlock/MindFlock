"""Shared test object factories.

A single place that knows how to build the pipeline's core dataclasses with
sensible, valid defaults so tests don't hand-repeat every required field. Keep
these importable (not pytest fixtures) so property tests, unit tests and
integration tests can all reuse them without a fixture dependency.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.ticket_ingestion.models import Ticket


def make_ticket(**overrides) -> Ticket:
    """A valid :class:`Ticket` with sensible defaults; override any field.

    Every required Ticket field has a default here, so a bare ``make_ticket()``
    is always constructible. Pass keyword overrides to set any field (required
    or optional, e.g. ``slug=``, ``comments=``, ``state=``); ``app_url``
    defaults to the Shortcut story URL for the effective ``id`` unless given.
    """
    fields: dict = {
        "id": 1,
        "name": "Test Story",
        "description": "A test story description that is long enough",
        "acceptance_criteria": ["Some criterion"],
        "owner_ids": ["member-123"],
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }
    fields.update(overrides)
    fields.setdefault("app_url", f"https://app.shortcut.com/story/{fields['id']}")
    return Ticket(**fields)
