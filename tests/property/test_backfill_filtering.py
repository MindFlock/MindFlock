# Feature: ticket-ingestion-pipeline, Property 5: Branch existence filtering
# Feature: ticket-ingestion-pipeline, Property 6: Chronological ordering
"""Property-based tests for backfill filtering and ordering.

**Validates: Requirements 3.3, 3.4**

Property 5: For any set of stories and any set of existing branch names, the backfill
filter SHALL include a story if and only if no existing branch starts with the prefix
`feature/sc-<story_id>/` for that story.

Property 6: For any set of stories with arbitrary creation timestamps, the backfill
scanner SHALL enqueue them in ascending order of creation time (oldest first).
"""

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis.strategies import (
    booleans,
    datetimes,
    integers,
    lists,
    timezones,
)

from backend.ticket_ingestion.backfill import (
    filter_stories_by_branches,
    sort_stories_chronologically,
)
from backend.ticket_ingestion.models import Ticket
from tests._factories import make_ticket

# --- Strategies ---

# Unique story IDs (positive integers)
story_ids = integers(min_value=1, max_value=1_000_000)

# Timestamps for created_at (timezone-aware)
story_datetimes = datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=timezones(),
)


# --- Helper ---


def make_story(story_id: int, created_at: datetime) -> Ticket:
    """Create a minimal Ticket with the given id and created_at."""
    return make_ticket(
        id=story_id,
        name=f"Story {story_id}",
        owner_ids=["member-1"],
        created_at=created_at,
    )


# --- Property 5: Branch Existence Filtering ---


@settings(max_examples=100)
@given(
    story_ids_list=lists(story_ids, min_size=0, max_size=20, unique=True),
    has_branch_flags=lists(booleans(), min_size=0, max_size=20),
)
def test_branch_existence_filtering(
    story_ids_list: list[int],
    has_branch_flags: list[bool],
) -> None:
    """For any set of stories and existing branch names, a story is included iff
    no branch matches `shortcut/<story_id>`."""
    # Align flags to story count
    flags = has_branch_flags[: len(story_ids_list)]
    while len(flags) < len(story_ids_list):
        flags.append(False)

    # Build stories with a fixed timestamp (ordering not relevant here)
    fixed_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    stories = [make_story(sid, fixed_time) for sid in story_ids_list]

    # Build existing branches set based on flags.
    # Contract: a story is filtered out iff some branch starts with
    # the prefix `feature/sc-<story_id>/` (see filter_stories_by_branches).
    existing_branches: set[str] = set()
    for sid, has_branch in zip(story_ids_list, flags):
        if has_branch:
            existing_branches.add(f"feature/sc-{sid}/work-{sid}")

    # Also add some unrelated branches to ensure they don't interfere
    existing_branches.add("main")
    existing_branches.add("feature/unrelated")

    # Run the filter
    result = filter_stories_by_branches(stories, existing_branches)

    # Assert: a story is included iff its branch does NOT exist
    result_ids = {s.id for s in result}
    for sid, has_branch in zip(story_ids_list, flags):
        if has_branch:
            assert sid not in result_ids, (
                f"Story {sid} should be excluded because a branch with prefix "
                f"'feature/sc-{sid}/' exists"
            )
        else:
            assert sid in result_ids, (
                f"Story {sid} should be included because no branch with prefix "
                f"'feature/sc-{sid}/' exists"
            )


# --- Property 6: Chronological Ordering ---


@settings(max_examples=100)
@given(
    story_data=lists(
        story_ids.flatmap(lambda sid: story_datetimes.map(lambda dt: (sid, dt))),
        min_size=0,
        max_size=20,
        unique_by=lambda x: x[0],  # unique story IDs
    ),
)
def test_chronological_ordering(
    story_data: list[tuple[int, datetime]],
) -> None:
    """For any set of stories with arbitrary creation timestamps, the sort function
    SHALL return them in ascending order of created_at (oldest first)."""
    # Build stories from generated data
    stories = [make_story(sid, created_at) for sid, created_at in story_data]

    # Sort using the function under test
    sorted_stories = sort_stories_chronologically(stories)

    # Assert: result is in ascending order of created_at
    for i in range(len(sorted_stories) - 1):
        assert sorted_stories[i].created_at <= sorted_stories[i + 1].created_at, (
            f"Stories not in chronological order: "
            f"story {sorted_stories[i].id} (created_at={sorted_stories[i].created_at}) "
            f"should come before "
            f"story {sorted_stories[i + 1].id} (created_at={sorted_stories[i + 1].created_at})"
        )

    # Assert: all original stories are present (no stories lost or duplicated)
    assert len(sorted_stories) == len(stories)
    assert {s.id for s in sorted_stories} == {s.id for s in stories}
