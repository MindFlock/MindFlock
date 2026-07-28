"""Unit tests for the AssigneeFilter."""

import logging
from datetime import datetime, timezone

import pytest

from backend.ticket_ingestion.filter import AssigneeFilter
from backend.ticket_ingestion.models import Ticket
from tests._factories import make_ticket


def _make_story(owner_ids: list[str], story_id: int = 1) -> Ticket:
    """Helper to create a Ticket with given owner_ids."""
    return make_ticket(
        id=story_id,
        description="A test story description",
        acceptance_criteria=["WHEN x THEN y"],
        owner_ids=owner_ids,
        app_url="https://app.shortcut.com/story/1",
        created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


TARGET_MEMBER_ID = "56d8a839-1234-5678-9abc-def012345678"


class TestAssigneeFilter:
    """Tests for AssigneeFilter.is_assigned."""

    def test_rejects_bare_string_member_ids(self) -> None:
        """A bare string is a common footgun (iterating it yields characters);
        the constructor rejects it rather than filtering on single letters."""
        with pytest.raises(TypeError, match="iterable of ids, not a str"):
            AssigneeFilter(TARGET_MEMBER_ID)

    def test_none_member_ids_accepts_everything(self) -> None:
        """No configured ids -> the provider already filtered server-side, so
        the filter is a pass-through even for an unassigned story."""
        filt = AssigneeFilter(None)
        assert filt.is_assigned(_make_story(owner_ids=[])) is True

    def test_assigned_to_target(self) -> None:
        """Story assigned to target member returns True."""
        filt = AssigneeFilter([TARGET_MEMBER_ID])
        story = _make_story(owner_ids=[TARGET_MEMBER_ID])
        assert filt.is_assigned(story) is True

    def test_not_assigned_to_target(self) -> None:
        """Story not assigned to target member returns False."""
        filt = AssigneeFilter([TARGET_MEMBER_ID])
        story = _make_story(owner_ids=["other-member-id"])
        assert filt.is_assigned(story) is False

    def test_empty_owner_ids(self) -> None:
        """Story with no owners returns False."""
        filt = AssigneeFilter([TARGET_MEMBER_ID])
        story = _make_story(owner_ids=[])
        assert filt.is_assigned(story) is False

    def test_multiple_owners_including_target(self) -> None:
        """Story with multiple owners including target returns True."""
        filt = AssigneeFilter([TARGET_MEMBER_ID])
        story = _make_story(owner_ids=["other-1", TARGET_MEMBER_ID, "other-2"])
        assert filt.is_assigned(story) is True

    def test_multiple_owners_excluding_target(self) -> None:
        """Story with multiple owners but not target returns False."""
        filt = AssigneeFilter([TARGET_MEMBER_ID])
        story = _make_story(owner_ids=["other-1", "other-2", "other-3"])
        assert filt.is_assigned(story) is False

    def test_logs_reason_when_filtered_out(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Filtering out a story logs the reason."""
        filt = AssigneeFilter([TARGET_MEMBER_ID])
        story = _make_story(owner_ids=["other-member"], story_id=42)

        with caplog.at_level(logging.INFO):
            result = filt.is_assigned(story)

        assert result is False
        assert "42" in caplog.text
        assert "not assigned to target member" in caplog.text
        assert TARGET_MEMBER_ID in caplog.text

    def test_no_log_when_assigned(self, caplog: pytest.LogCaptureFixture) -> None:
        """No log message when story is assigned to target."""
        filt = AssigneeFilter([TARGET_MEMBER_ID])
        story = _make_story(owner_ids=[TARGET_MEMBER_ID])

        with caplog.at_level(logging.INFO):
            result = filt.is_assigned(story)

        assert result is True
        assert "filtered out" not in caplog.text
