"""Unit tests for the backfill scanner module."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.ticket_ingestion.backfill import (
    BackfillScanner,
    _MAX_RETRIES,
    filter_stories_by_branches,
    sort_stories_chronologically,
)
from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion.providers.base import (
    parse_acceptance_criteria as _parse_acceptance_criteria,
)
from backend.ticket_ingestion.providers.shortcut import (
    story_from_api_response as _story_from_api_response,
)
from backend.ticket_ingestion.models import Ticket
from tests._factories import make_ticket


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point the scanner's ledger at a scratch dir.

    ``scan`` now writes pending-story markers (crash recovery) in addition to
    reading the processed ledger; without this the tests would write
    ``./state.json`` in whatever cwd pytest runs in.
    """
    from backend.ticket_ingestion import backfill as backfill_mod

    monkeypatch.setattr(backfill_mod, "_STATE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def config():
    """Create a test PipelineConfig."""
    return PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut",
            api_token="test-token",
            member_id="member-123",
        ),
        repo_url="git@github.com:org/repo.git",
        workspace_dir=__import__("pathlib").Path("/tmp/workspaces"),
        min_description_length=20,
        log_file=__import__("pathlib").Path("/tmp/pipeline.log"),
        log_level="INFO",
    )


@pytest.fixture
def queue():
    """Create an asyncio queue for testing."""
    return asyncio.Queue()


def _make_story(story_id: int, created_at: datetime | None = None) -> Ticket:
    """Helper to create a Ticket for testing."""
    return make_ticket(
        id=story_id,
        name=f"Story {story_id}",
        description="Test description with enough length",
        acceptance_criteria=["criterion 1"],
        created_at=created_at or datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


class TestParseAcceptanceCriteria:
    """Tests for _parse_acceptance_criteria."""

    def test_empty_description(self):
        assert _parse_acceptance_criteria("") == []

    def test_bullet_points_under_ac_header(self):
        desc = """## Acceptance Criteria
- First criterion
- Second criterion
"""
        result = _parse_acceptance_criteria(desc)
        assert "First criterion" in result
        assert "Second criterion" in result

    def test_numbered_items_under_ac_header(self):
        desc = """## Acceptance Criteria
1. First item
2. Second item
"""
        result = _parse_acceptance_criteria(desc)
        assert "First item" in result
        assert "Second item" in result

    def test_when_then_patterns(self):
        desc = """WHEN a user logs in
THEN they see the dashboard
"""
        result = _parse_acceptance_criteria(desc)
        assert any("WHEN" in c for c in result)
        assert any("THEN" in c for c in result)

    def test_asterisk_bullets(self):
        desc = """## Acceptance Criteria
* First criterion
* Second criterion
"""
        result = _parse_acceptance_criteria(desc)
        assert "First criterion" in result
        assert "Second criterion" in result

    def test_stops_at_next_header(self):
        desc = """## Acceptance Criteria
- Criterion one
## Implementation Notes
- Not a criterion
"""
        result = _parse_acceptance_criteria(desc)
        assert "Criterion one" in result
        assert "Not a criterion" not in result

    def test_global_bullets_when_no_ac_section(self):
        desc = """Some description text.
- First bullet
- Second bullet
"""
        result = _parse_acceptance_criteria(desc)
        assert "First bullet" in result
        assert "Second bullet" in result


class TestStoryFromApiResponse:
    """Tests for _story_from_api_response."""

    def test_basic_story_parsing(self):
        data = {
            "id": 12345,
            "name": "Test Story",
            "description": "A test story description\n- criterion 1",
            "owner_ids": ["uuid-1", "uuid-2"],
            "app_url": "https://app.shortcut.com/story/12345",
            "created_at": "2025-01-15T10:30:00Z",
        }
        story = _story_from_api_response(data)
        assert story.id == 12345
        assert story.name == "Test Story"
        assert story.owner_ids == ["uuid-1", "uuid-2"]
        assert story.app_url == "https://app.shortcut.com/story/12345"
        assert story.created_at == datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_missing_optional_fields(self):
        data = {
            "id": 99,
            "created_at": "2025-01-01T00:00:00Z",
        }
        story = _story_from_api_response(data)
        assert story.id == 99
        assert story.name == ""
        assert story.description == ""
        assert story.owner_ids == []

    def test_null_description(self):
        data = {
            "id": 100,
            "name": "Null desc",
            "description": None,
            "owner_ids": [],
            "app_url": "",
            "created_at": "2025-06-01T12:00:00Z",
        }
        story = _story_from_api_response(data)
        assert story.description == ""


class TestFilterStoriesByBranches:
    """Tests for filter_stories_by_branches."""

    def test_no_existing_branches(self):
        stories = [_make_story(1), _make_story(2)]
        result = filter_stories_by_branches(stories, set())
        assert len(result) == 2

    def test_all_branches_exist(self):
        stories = [_make_story(1), _make_story(2)]
        branches = {"feature/sc-1/some-work", "feature/sc-2/other-work"}
        result = filter_stories_by_branches(stories, branches)
        assert len(result) == 0

    def test_partial_branches_exist(self):
        stories = [_make_story(1), _make_story(2), _make_story(3)]
        branches = {"feature/sc-2/some-work"}
        result = filter_stories_by_branches(stories, branches)
        assert len(result) == 2
        assert all(s.id != 2 for s in result)

    def test_unrelated_branches_ignored(self):
        stories = [_make_story(1)]
        branches = {"feature/something", "main", "develop"}
        result = filter_stories_by_branches(stories, branches)
        assert len(result) == 1


class TestSortStoriesChronologically:
    """Tests for sort_stories_chronologically."""

    def test_already_sorted(self):
        stories = [
            _make_story(1, datetime(2025, 1, 1, tzinfo=timezone.utc)),
            _make_story(2, datetime(2025, 1, 2, tzinfo=timezone.utc)),
            _make_story(3, datetime(2025, 1, 3, tzinfo=timezone.utc)),
        ]
        result = sort_stories_chronologically(stories)
        assert [s.id for s in result] == [1, 2, 3]

    def test_reverse_order(self):
        stories = [
            _make_story(3, datetime(2025, 1, 3, tzinfo=timezone.utc)),
            _make_story(2, datetime(2025, 1, 2, tzinfo=timezone.utc)),
            _make_story(1, datetime(2025, 1, 1, tzinfo=timezone.utc)),
        ]
        result = sort_stories_chronologically(stories)
        assert [s.id for s in result] == [1, 2, 3]

    def test_empty_list(self):
        assert sort_stories_chronologically([]) == []

    def test_single_story(self):
        stories = [_make_story(1)]
        result = sort_stories_chronologically(stories)
        assert len(result) == 1


class TestBackfillScanner:
    """Tests for the BackfillScanner class."""

    @pytest.fixture
    def scanner(self, config, queue):
        return BackfillScanner(config, queue)

    async def test_scan_no_stories_found(self, scanner, queue, tmp_path):
        """Test scan when API returns no stories."""
        with (
            patch(
                "backend.ticket_ingestion.backfill.load_last_run_timestamp",
                return_value=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            patch(
                "backend.ticket_ingestion.backfill.save_last_run_timestamp",
            ) as mock_save,
            patch.object(
                scanner, "_fetch_stories", new_callable=AsyncMock
            ) as mock_fetch,
        ):
            mock_fetch.return_value = []
            result = await scanner.scan()

        assert result == 0
        assert queue.empty()
        mock_save.assert_called_once()

    async def test_scan_enqueues_eligible_stories(self, scanner, queue, tmp_path):
        """Test scan enqueues stories that don't have branches."""
        stories = [
            _make_story(1, datetime(2025, 1, 2, tzinfo=timezone.utc)),
            _make_story(2, datetime(2025, 1, 1, tzinfo=timezone.utc)),
        ]

        with (
            patch(
                "backend.ticket_ingestion.backfill.load_last_run_timestamp",
                return_value=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            patch(
                "backend.ticket_ingestion.backfill.save_last_run_timestamp",
            ),
            patch.object(
                scanner, "_fetch_stories", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "backend.ticket_ingestion.backfill._get_existing_branches",
                new_callable=AsyncMock,
            ) as mock_branches,
        ):
            mock_fetch.return_value = stories
            mock_branches.return_value = set()
            result = await scanner.scan()

        assert result == 2
        # Should be enqueued oldest-first
        first = await queue.get()
        second = await queue.get()
        assert first.id == 2  # older
        assert second.id == 1  # newer

    async def test_scan_skips_in_flight_story(
        self, scanner, queue, tmp_path, monkeypatch
    ):
        """Idempotency (TOCTOU fix): a story whose session is still running has
        an ``in_flight`` ledger entry, and a scan during that window must not
        re-enqueue it — previously the record only appeared after completion."""
        from backend.ticket_ingestion import backfill as backfill_mod
        from backend.ticket_ingestion.models import ProcessingRecord
        from backend.ticket_ingestion.state import record_processed_story

        monkeypatch.setattr(backfill_mod, "_STATE_DIR", tmp_path)
        record_processed_story(
            tmp_path,
            ProcessingRecord(
                story_id="sc-1",
                branch="sc-1",
                status="in_flight",
                processed_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
            ),
        )

        with (
            patch.object(
                scanner, "_fetch_stories", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "backend.ticket_ingestion.backfill._get_existing_branches",
                new_callable=AsyncMock,
            ) as mock_branches,
        ):
            mock_fetch.return_value = [_make_story(1), _make_story(2)]
            mock_branches.return_value = set()  # no branch exists yet!
            result = await scanner.scan()

        # Only the not-in-flight story is enqueued.
        assert result == 1
        assert (await queue.get()).id == 2
        assert queue.empty()

    async def test_scan_stamps_source_repo_url(self, config, queue):
        """Multi-repo ingestion: each enqueued ticket carries its source's repo
        so the provisioner clones the right repo (not the global default)."""
        from backend.ticket_ingestion.config import TicketProviderConfig

        source = TicketProviderConfig(
            provider="shortcut",
            member_id="member-123",
            repo_url="git@github.com:org/api.git",
        )
        scanner = BackfillScanner(config, queue, source)
        with (
            patch(
                "backend.ticket_ingestion.backfill.load_last_run_timestamp",
                return_value=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            patch("backend.ticket_ingestion.backfill.save_last_run_timestamp"),
            patch.object(
                scanner, "_fetch_stories", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "backend.ticket_ingestion.backfill._get_existing_branches",
                new_callable=AsyncMock,
            ) as mock_branches,
        ):
            mock_fetch.return_value = [_make_story(1)]
            mock_branches.return_value = set()
            # The branch-existence check must target the source repo, not global.
            await scanner.scan()
            assert mock_branches.call_args.args[0] == "git@github.com:org/api.git"

        enqueued = await queue.get()
        assert enqueued.repo_url == "git@github.com:org/api.git"

    async def test_scan_filters_existing_branches(self, scanner, queue):
        """Test scan filters out stories with existing branches."""
        stories = [
            _make_story(1, datetime(2025, 1, 1, tzinfo=timezone.utc)),
            _make_story(2, datetime(2025, 1, 2, tzinfo=timezone.utc)),
        ]

        with (
            patch(
                "backend.ticket_ingestion.backfill.load_last_run_timestamp",
                return_value=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            patch(
                "backend.ticket_ingestion.backfill.save_last_run_timestamp",
            ),
            patch.object(
                scanner, "_fetch_stories", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "backend.ticket_ingestion.backfill._get_existing_branches",
                new_callable=AsyncMock,
            ) as mock_branches,
        ):
            mock_fetch.return_value = stories
            mock_branches.return_value = {"feature/sc-1/some-work"}
            result = await scanner.scan()

        assert result == 1
        enqueued = await queue.get()
        assert enqueued.id == 2

    async def test_scan_retries_on_api_failure(self, scanner, queue):
        """Test scan retries with exponential backoff on API failure."""
        import aiohttp

        with (
            patch(
                "backend.ticket_ingestion.backfill.load_last_run_timestamp",
                return_value=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            patch.object(
                scanner, "_fetch_stories", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "backend.ticket_ingestion.backfill.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            mock_fetch.side_effect = aiohttp.ClientError("Connection failed")
            result = await scanner.scan()

        assert result == 0
        assert mock_fetch.call_count == _MAX_RETRIES
        assert queue.empty()

    async def test_scan_retries_on_timeout(self, scanner, queue):
        """aiohttp's total-timeout raises asyncio.TimeoutError — it must hit
        the same backoff loop as ClientError, not escape the scan."""
        with (
            patch(
                "backend.ticket_ingestion.backfill.load_last_run_timestamp",
                return_value=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            patch(
                "backend.ticket_ingestion.backfill.save_last_run_timestamp",
            ),
            patch.object(
                scanner, "_fetch_stories", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "backend.ticket_ingestion.backfill._get_existing_branches",
                new_callable=AsyncMock,
            ) as mock_branches,
            patch(
                "backend.ticket_ingestion.backfill.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            mock_fetch.side_effect = [
                asyncio.TimeoutError(),
                [_make_story(1)],
            ]
            mock_branches.return_value = set()
            result = await scanner.scan()

        assert result == 1
        assert mock_fetch.call_count == 2

    async def test_scan_records_pending_marker_before_checkpoint(
        self, scanner, queue, _isolated_state_dir
    ):
        """Crash safety: each enqueued ticket gets a pending marker in the
        ledger BEFORE the poll checkpoint advances, so a crash with a
        non-empty in-memory queue can't lose it forever."""
        from backend.ticket_ingestion.state import load_pending_stories

        with (
            patch(
                "backend.ticket_ingestion.backfill.load_last_run_timestamp",
                return_value=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            patch.object(
                scanner, "_fetch_stories", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "backend.ticket_ingestion.backfill._get_existing_branches",
                new_callable=AsyncMock,
            ) as mock_branches,
        ):
            mock_fetch.return_value = [_make_story(7)]
            mock_branches.return_value = set()
            result = await scanner.scan()

        assert result == 1
        pending = load_pending_stories(_isolated_state_dir)
        assert [e["story_id"] for e in pending] == ["sc-7"]
        assert pending[0]["ticket_id"] == "7"

    async def test_scan_skips_already_pending_story(
        self, scanner, queue, _isolated_state_dir
    ):
        """A ticket still sitting in the queue (pending marker present) is not
        enqueued a second time by the next poll scan."""
        from backend.ticket_ingestion.state import record_pending_story

        record_pending_story(_isolated_state_dir, "sc-1", 1, None)
        with (
            patch(
                "backend.ticket_ingestion.backfill.load_last_run_timestamp",
                return_value=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            patch.object(
                scanner, "_fetch_stories", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "backend.ticket_ingestion.backfill._get_existing_branches",
                new_callable=AsyncMock,
            ) as mock_branches,
        ):
            mock_fetch.return_value = [_make_story(1), _make_story(2)]
            mock_branches.return_value = set()
            result = await scanner.scan()

        assert result == 1
        assert (await queue.get()).id == 2
        assert queue.empty()

    async def test_scan_succeeds_after_retry(self, scanner, queue):
        """Test scan succeeds after initial failures."""
        import aiohttp

        stories = [_make_story(1)]

        with (
            patch(
                "backend.ticket_ingestion.backfill.load_last_run_timestamp",
                return_value=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            patch(
                "backend.ticket_ingestion.backfill.save_last_run_timestamp",
            ),
            patch.object(
                scanner, "_fetch_stories", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "backend.ticket_ingestion.backfill._get_existing_branches",
                new_callable=AsyncMock,
            ) as mock_branches,
            patch(
                "backend.ticket_ingestion.backfill.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            # Fail twice, then succeed
            mock_fetch.side_effect = [
                aiohttp.ClientError("fail 1"),
                aiohttp.ClientError("fail 2"),
                stories,
            ]
            mock_branches.return_value = set()
            result = await scanner.scan()

        assert result == 1
        assert mock_fetch.call_count == 3
