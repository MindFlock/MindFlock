"""Unit tests for the TicketValidator."""

from datetime import datetime, timezone
from pathlib import Path


from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion.models import Ticket
from backend.ticket_ingestion.validator import TicketValidator
from tests._factories import make_ticket


def _make_config(min_description_length: int = 20) -> PipelineConfig:
    """Helper to create a minimal PipelineConfig with configurable min_description_length."""
    return PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut",
            api_token="test-token",
            member_id="member-123",
        ),
        repo_url="https://github.com/test/repo",
        workspace_dir=Path("/tmp/workspaces"),
        min_description_length=min_description_length,
        log_file=Path("/tmp/pipeline.log"),
        log_level="INFO",
    )


def _make_story(
    description: str = "A valid description that is long enough for validation",
    acceptance_criteria: list[str] | None = None,
) -> Ticket:
    """Helper to create a Ticket with configurable description and acceptance_criteria."""
    if acceptance_criteria is None:
        acceptance_criteria = ["WHEN user clicks submit THEN form is saved"]
    return make_ticket(
        id=101,
        description=description,
        acceptance_criteria=acceptance_criteria,
        created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


class TestTicketValidatorDescription:
    """Tests for description validation (Requirement 4.1)."""

    def test_exactly_20_character_description_passes(self) -> None:
        """A description of exactly 20 characters should pass validation."""
        config = _make_config(min_description_length=20)
        validator = TicketValidator(config)
        # Exactly 20 characters
        story = _make_story(description="12345678901234567890")
        result = validator.validate(story)
        assert result.is_valid is True
        assert result.failures == []

    def test_19_character_description_fails(self) -> None:
        """A description of 19 characters (one below minimum) should fail."""
        config = _make_config(min_description_length=20)
        validator = TicketValidator(config)
        story = _make_story(description="1234567890123456789")
        result = validator.validate(story)
        assert result.is_valid is False
        assert len(result.failures) == 1
        assert "too short" in result.failures[0]

    def test_empty_description_fails(self) -> None:
        """An empty description should fail validation."""
        config = _make_config(min_description_length=20)
        validator = TicketValidator(config)
        story = _make_story(description="")
        result = validator.validate(story)
        assert result.is_valid is False
        assert len(result.failures) == 1
        assert "empty" in result.failures[0].lower()

    def test_whitespace_only_description_fails(self) -> None:
        """A description with only whitespace should fail as empty."""
        config = _make_config(min_description_length=20)
        validator = TicketValidator(config)
        story = _make_story(description="     \t\n   ")
        result = validator.validate(story)
        assert result.is_valid is False
        assert any("empty" in f.lower() for f in result.failures)

    def test_description_with_leading_trailing_whitespace_stripped(self) -> None:
        """Description length is checked after stripping whitespace."""
        config = _make_config(min_description_length=20)
        validator = TicketValidator(config)
        # 19 real chars + surrounding whitespace = still too short
        story = _make_story(description="   1234567890123456789   ")
        result = validator.validate(story)
        assert result.is_valid is False
        assert any("too short" in f for f in result.failures)


class TestTicketValidatorAcceptanceCriteria:
    """Tests for acceptance criteria validation (Requirement 4.2)."""

    def test_when_then_format_passes(self) -> None:
        """WHEN/THEN format acceptance criteria should pass."""
        config = _make_config()
        validator = TicketValidator(config)
        story = _make_story(
            acceptance_criteria=["WHEN user logs in THEN dashboard is shown"]
        )
        result = validator.validate(story)
        assert result.is_valid is True

    def test_given_when_then_format_passes(self) -> None:
        """Given/When/Then format acceptance criteria should pass."""
        config = _make_config()
        validator = TicketValidator(config)
        story = _make_story(
            acceptance_criteria=[
                "Given a logged-in user, When they click settings, Then preferences are shown"
            ]
        )
        result = validator.validate(story)
        assert result.is_valid is True

    def test_bullet_point_format_passes(self) -> None:
        """Bullet point style acceptance criteria should pass."""
        config = _make_config()
        validator = TicketValidator(config)
        story = _make_story(
            acceptance_criteria=[
                "- User can create a new account",
                "- User receives confirmation email",
            ]
        )
        result = validator.validate(story)
        assert result.is_valid is True

    def test_missing_acceptance_criteria_fails(self) -> None:
        """An empty acceptance criteria list should fail validation."""
        config = _make_config()
        validator = TicketValidator(config)
        story = _make_story(acceptance_criteria=[])
        result = validator.validate(story)
        assert result.is_valid is False
        assert any("acceptance criteria" in f.lower() for f in result.failures)


class TestTicketValidatorCombined:
    """Tests for combined validation failures (Requirement 4.3)."""

    def test_both_failures_reported(self) -> None:
        """Both empty description and missing criteria produce two failures."""
        config = _make_config()
        validator = TicketValidator(config)
        story = _make_story(description="", acceptance_criteria=[])
        result = validator.validate(story)
        assert result.is_valid is False
        assert len(result.failures) == 2

    def test_valid_story_passes(self) -> None:
        """A story with sufficient description and criteria passes."""
        config = _make_config()
        validator = TicketValidator(config)
        story = _make_story()
        result = validator.validate(story)
        assert result.is_valid is True
        assert result.failures == []

    def test_failures_are_human_readable(self) -> None:
        """Failure messages should be human-readable strings."""
        config = _make_config()
        validator = TicketValidator(config)
        story = _make_story(description="short", acceptance_criteria=[])
        result = validator.validate(story)
        assert result.is_valid is False
        for failure in result.failures:
            assert isinstance(failure, str)
            assert len(failure) > 10  # Not just a code or abbreviation
