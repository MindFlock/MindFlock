# Feature: ticket-ingestion-pipeline, Property 7: Ticket validation correctness
# Feature: ticket-ingestion-pipeline, Property 8: Re-validation with supplemental context
"""Property-based tests for ticket validation.

**Validates: Requirements 4.1, 4.2, 4.3, 4.5**

Property 7: For any Shortcut story, the ticket validator SHALL return valid if and only
if the description is at least 20 characters long (after stripping) AND the story
contains at least one acceptance criterion. When validation fails, the result SHALL
contain a non-empty list of human-readable failure reasons that specifically identify
which checks failed.

Property 8: For any Shortcut story and any supplemental context string, re-validating
the story with the supplemental context appended to the description SHALL produce the
same result as validating a story whose description is the concatenation of the original
description and the supplemental context.
"""

from hypothesis import given, settings
from hypothesis.strategies import lists, text

from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion.models import Ticket
from backend.ticket_ingestion.validator import TicketValidator
from tests._factories import make_ticket

# --- Helpers ---


def make_config() -> PipelineConfig:
    """Create a minimal PipelineConfig with min_description_length=20."""
    return PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut",
            api_token="test-token",
            member_id="test-member-id",
        ),
        repo_url="git@github.com:org/repo.git",
        workspace_dir="./workspaces",
        min_description_length=20,
        log_file="./logs/pipeline.log",
        log_level="INFO",
    )


def make_story(description: str, acceptance_criteria: list[str]) -> Ticket:
    """Create a minimal Ticket with the given description and acceptance criteria."""
    return make_ticket(
        description=description,
        acceptance_criteria=acceptance_criteria,
        owner_ids=["owner-1"],
    )


# --- Strategies ---

# Descriptions of varying lengths (including whitespace-only and empty)
arbitrary_description = text(min_size=0, max_size=200)

# Non-empty text for acceptance criteria items
non_empty_criterion = text(min_size=1, max_size=100)

# Lists of acceptance criteria (can be empty)
acceptance_criteria_lists = lists(non_empty_criterion, min_size=0, max_size=5)

# --- Property 7: Ticket Validation Correctness ---


@settings(max_examples=100)
@given(
    description=arbitrary_description,
    acceptance_criteria=acceptance_criteria_lists,
)
def test_ticket_validation_correctness(
    description: str,
    acceptance_criteria: list[str],
) -> None:
    """For any Shortcut story, the ticket validator SHALL return valid if and only if
    the description is at least 20 characters long (after stripping) AND the story
    contains at least one acceptance criterion. When validation fails, the result SHALL
    contain a non-empty list of failure reasons identifying which checks failed."""
    config = make_config()
    validator = TicketValidator(config)
    story = make_story(description, acceptance_criteria)

    result = validator.validate(story)

    stripped_description = description.strip()
    has_sufficient_description = len(stripped_description) >= 20
    has_acceptance_criteria = len(acceptance_criteria) > 0

    expected_valid = has_sufficient_description and has_acceptance_criteria

    # Assert valid iff both conditions are met
    assert result.is_valid == expected_valid, (
        f"Expected is_valid={expected_valid} for description length "
        f"{len(stripped_description)} (stripped) and "
        f"{len(acceptance_criteria)} acceptance criteria, got {result.is_valid}. "
        f"Failures: {result.failures}"
    )

    # Assert failures list is non-empty when validation fails
    if not result.is_valid:
        assert (
            len(result.failures) > 0
        ), "Expected non-empty failures list when validation fails"

    # Assert failures identify which checks failed
    if not has_sufficient_description and not result.is_valid:
        description_failure_found = any(
            "description" in f.lower() or "character" in f.lower()
            for f in result.failures
        )
        assert (
            description_failure_found
        ), f"Expected a failure mentioning description length, got: {result.failures}"

    if not has_acceptance_criteria and not result.is_valid:
        criteria_failure_found = any(
            "acceptance criteria" in f.lower() or "criterion" in f.lower()
            for f in result.failures
        )
        assert (
            criteria_failure_found
        ), f"Expected a failure mentioning acceptance criteria, got: {result.failures}"

    # Assert failures list is empty when validation passes
    if result.is_valid:
        assert (
            len(result.failures) == 0
        ), f"Expected empty failures list when validation passes, got: {result.failures}"
