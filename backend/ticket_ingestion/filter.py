"""Assignee filter.

A defensive net on top of each provider's server-side "assigned to me" search:
it keeps a ticket only when the configured member id is among its assignees.
When no member id is configured (some providers resolve "me" implicitly from the
token, e.g. GitHub/Asana), the filter is a no-op and lets everything through —
the provider already scoped the search.
"""

import logging
from typing import Iterable

from backend.ticket_ingestion.models import Ticket

_logger = logging.getLogger(__name__)


class AssigneeFilter:
    def __init__(self, member_ids: Iterable[object]) -> None:
        # An iterable of member ids (multi-source: a ticket passes if assigned
        # to ANY configured identity). Empty => accept everything (the provider
        # already filtered server-side by the authenticated user).
        if isinstance(member_ids, str):
            raise TypeError("member_ids must be an iterable of ids, not a str")
        self._member_ids = {str(m) for m in (member_ids or []) if m}

    def is_assigned(self, story: Ticket) -> bool:
        if not self._member_ids:
            return True  # provider already filtered by the authenticated user
        if self._member_ids.intersection(story.owner_ids):
            return True
        _logger.info(
            "Ticket %s not assigned to target member(s) %s; owners=%s",
            story.slug,
            sorted(self._member_ids),
            story.owner_ids,
        )
        return False
