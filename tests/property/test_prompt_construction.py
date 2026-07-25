# Feature: ticket-ingestion-pipeline, Property 9: Prompt completeness
"""Property-based tests for prompt construction.

**Validates: Requirements 6.2, 6.4, 6.5**

Property 9: For any Shortcut story with a title, description, and acceptance criteria,
the constructed Claude Code prompt SHALL contain the story title, the full description,
all acceptance criteria, the standing ticket-workflow instructions, and the expected
section headers. If supplemental context is provided, it SHALL also appear in the prompt
under its own section.
"""

from hypothesis import given, settings
from hypothesis.strategies import lists, none, one_of, text

from backend.ticket_ingestion.claude_runner import ClaudeCodeRunner
from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion.models import Ticket
from tests._factories import make_ticket

# --- Helpers ---


def make_config() -> PipelineConfig:
    """Create a minimal PipelineConfig for testing."""
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


def make_story(name: str, description: str, acceptance_criteria: list[str]) -> Ticket:
    """Create a Ticket with the given fields."""
    return make_ticket(
        id=42,
        name=name,
        description=description,
        acceptance_criteria=acceptance_criteria,
        owner_ids=["owner-1"],
    )


# --- Strategies ---

# Non-empty text for story name
story_name = text(min_size=1, max_size=200)

# Arbitrary description text
story_description = text(min_size=0, max_size=500)

# Non-empty text for individual acceptance criteria
non_empty_criterion = text(min_size=1, max_size=200)

# Lists of acceptance criteria (non-empty)
acceptance_criteria_lists = lists(non_empty_criterion, min_size=1, max_size=10)

# Optional supplemental context: either None or non-empty text
supplemental_context_strategy = one_of(none(), text(min_size=1, max_size=300))


# --- Property 9: Prompt Completeness ---


@settings(max_examples=100)
@given(
    name=story_name,
    description=story_description,
    acceptance_criteria=acceptance_criteria_lists,
    supplemental_context=supplemental_context_strategy,
)
def test_prompt_completeness(
    name: str,
    description: str,
    acceptance_criteria: list[str],
    supplemental_context: str | None,
) -> None:
    """For any Shortcut story with a title, description, and acceptance criteria,
    the constructed prompt SHALL contain the story title, full description, all
    acceptance criteria, the standing ticket-workflow instructions, and the
    expected section headers. When supplemental context is provided, it SHALL
    also appear in the prompt under its own section."""
    config = make_config()
    runner = ClaudeCodeRunner(config)
    story = make_story(name, description, acceptance_criteria)

    prompt = runner._build_prompt(story, supplemental_context)

    # Assert the prompt contains the story title
    assert name in prompt, f"Prompt does not contain story title: {name!r}"

    # Assert the prompt contains the full description
    assert (
        description in prompt
    ), f"Prompt does not contain full description: {description!r}"

    # Assert the prompt contains all acceptance criteria
    for criterion in acceptance_criteria:
        assert (
            criterion in prompt
        ), f"Prompt does not contain acceptance criterion: {criterion!r}"

    # Assert the prompt contains the story header section
    assert f"# Story: {name}" in prompt, "Prompt does not contain the story header"

    # Assert the prompt contains the Shortcut URL
    assert story.app_url in prompt, "Prompt does not contain the Shortcut URL"

    # Assert the prompt contains the Description section header
    assert "## Description" in prompt, "Prompt does not contain the Description section"

    # Assert the prompt contains the Acceptance Criteria section header
    assert (
        "## Acceptance Criteria" in prompt
    ), "Prompt does not contain the Acceptance Criteria section"

    # Assert the prompt contains the standing ticket-workflow instructions
    assert (
        "Follow the ticket requirements closely" in prompt
    ), "Prompt does not contain the ticket workflow instructions"

    # When supplemental context is provided, assert it appears in the prompt
    # under its own section.
    if supplemental_context is not None:
        assert (
            "## Supplemental Context" in prompt
        ), "Prompt does not contain the Supplemental Context section header"
        assert (
            supplemental_context in prompt
        ), f"Prompt does not contain supplemental context: {supplemental_context!r}"
