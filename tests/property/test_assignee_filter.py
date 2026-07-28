# Feature: ticket-ingestion-pipeline, Property 3: Assignee filter correctness
"""Property-based tests for assignee filter correctness.

**Validates: Requirements 2.1**

Property 3: For any Shortcut story with any set of owner IDs, the assignee filter
SHALL return true if and only if the configured target member ID is present in the
story's owner IDs list.
"""

from hypothesis import given, settings
from hypothesis.strategies import booleans, lists, text

from backend.ticket_ingestion.filter import AssigneeFilter
from backend.ticket_ingestion.models import Ticket
from tests._factories import make_ticket

# --- Strategies ---

# Non-empty text for member IDs
non_empty_text = text(min_size=1, max_size=50)

# Lists of non-empty text for owner_ids
owner_id_lists = lists(non_empty_text, min_size=0, max_size=10)


# --- Helper ---


def make_story(owner_ids: list[str]) -> Ticket:
    """Create a minimal Ticket with the given owner_ids."""
    return make_ticket(description="A test story description", owner_ids=owner_ids)


# --- Property 3: Assignee Filter Correctness ---


@settings(max_examples=100)
@given(
    target_member_id=non_empty_text,
    owner_ids=owner_id_lists,
    include_target=booleans(),
)
def test_assignee_filter_returns_true_iff_target_in_owner_ids(
    target_member_id: str,
    owner_ids: list[str],
    include_target: bool,
) -> None:
    """For any Shortcut story with any set of owner IDs, the assignee filter SHALL
    return true if and only if the configured target member ID is present in the
    story's owner_ids list."""
    # Build the final owner_ids: conditionally include the target
    if include_target:
        final_owner_ids = owner_ids + [target_member_id]
    else:
        # Remove any accidental occurrences of target_member_id
        final_owner_ids = [oid for oid in owner_ids if oid != target_member_id]

    story = make_story(final_owner_ids)
    assignee_filter = AssigneeFilter([target_member_id])

    result = assignee_filter.is_assigned(story)

    # Assert: filter returns True iff target_member_id is in owner_ids
    expected = target_member_id in final_owner_ids
    assert result == expected, (
        f"Expected is_assigned={expected} for target={target_member_id!r} "
        f"in owner_ids={final_owner_ids!r}, got {result}"
    )
