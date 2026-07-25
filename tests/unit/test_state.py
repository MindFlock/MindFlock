"""Unit tests for state persistence (state.json read/write)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.ticket_ingestion.models import ProcessedIssue, ProcessingRecord
from backend.ticket_ingestion.state import (
    _EPOCH,
    clear_issue_attempts,
    clear_pr_attempts,
    load_last_run_timestamp,
    load_pending_stories,
    load_processed_issues,
    load_processed_story_statuses,
    reap_stale_in_flight,
    record_issue_attempt,
    record_pending_story,
    record_pr_attempt,
    record_processed_issue,
    record_processed_story,
    remove_pending_story,
    save_last_run_timestamp,
)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for state files."""
    return tmp_path


@pytest.fixture
def valid_state_file(state_dir: Path) -> Path:
    """Create a valid state.json file."""
    state_file = state_dir / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "last_run_timestamps": {"sc": "2025-01-15T10:30:00+00:00"},
                "processed_stories": [
                    {
                        "story_id": 12345,
                        "branch": "shortcut/12345",
                        "status": "completed",
                        "processed_at": "2025-01-15T10:35:00+00:00",
                    }
                ],
            }
        )
    )
    return state_file


class TestLoadLastRunTimestamp:
    """Tests for load_last_run_timestamp."""

    def test_loads_valid_timestamp(
        self, state_dir: Path, valid_state_file: Path
    ) -> None:
        ts = load_last_run_timestamp(state_dir, "sc")
        expected = datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert ts == expected

    def test_returns_epoch_for_missing_file(self, state_dir: Path) -> None:
        ts = load_last_run_timestamp(state_dir, "sc")
        assert ts == _EPOCH

    def test_returns_epoch_for_corrupted_json(self, state_dir: Path) -> None:
        state_file = state_dir / "state.json"
        state_file.write_text("not valid json {{{")
        ts = load_last_run_timestamp(state_dir, "sc")
        assert ts == _EPOCH

    def test_returns_epoch_for_empty_file(self, state_dir: Path) -> None:
        state_file = state_dir / "state.json"
        state_file.write_text("")
        ts = load_last_run_timestamp(state_dir, "sc")
        assert ts == _EPOCH

    def test_returns_epoch_for_non_dict_json(self, state_dir: Path) -> None:
        state_file = state_dir / "state.json"
        state_file.write_text(json.dumps([1, 2, 3]))
        ts = load_last_run_timestamp(state_dir, "sc")
        assert ts == _EPOCH

    def test_returns_epoch_for_missing_timestamp_field(self, state_dir: Path) -> None:
        state_file = state_dir / "state.json"
        state_file.write_text(json.dumps({"processed_stories": []}))
        ts = load_last_run_timestamp(state_dir, "sc")
        assert ts == _EPOCH

    def test_returns_epoch_for_invalid_timestamp_string(self, state_dir: Path) -> None:
        state_file = state_dir / "state.json"
        state_file.write_text(
            json.dumps(
                {"last_run_timestamps": {"sc": "not-a-date"}, "processed_stories": []}
            )
        )
        ts = load_last_run_timestamp(state_dir, "sc")
        assert ts == _EPOCH

    def test_returns_utc_aware_datetime(
        self, state_dir: Path, valid_state_file: Path
    ) -> None:
        ts = load_last_run_timestamp(state_dir, "sc")
        assert ts.tzinfo is not None

    def test_treats_naive_timestamp_as_utc(self, state_dir: Path) -> None:
        state_file = state_dir / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "last_run_timestamps": {"sc": "2025-01-15T10:30:00"},
                    "processed_stories": [],
                }
            )
        )
        ts = load_last_run_timestamp(state_dir, "sc")
        assert ts.tzinfo == timezone.utc
        assert ts == datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


class TestSaveLastRunTimestamp:
    """Tests for save_last_run_timestamp."""

    def test_saves_timestamp_to_new_file(self, state_dir: Path) -> None:
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        save_last_run_timestamp(state_dir, ts, "sc")

        state_file = state_dir / "state.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["last_run_timestamps"]["sc"] == "2025-06-01T12:00:00+00:00"

    def test_preserves_existing_processed_stories(
        self, state_dir: Path, valid_state_file: Path
    ) -> None:
        new_ts = datetime(2025, 2, 1, 8, 0, 0, tzinfo=timezone.utc)
        save_last_run_timestamp(state_dir, new_ts, "sc")

        data = json.loads(valid_state_file.read_text())
        assert data["last_run_timestamps"]["sc"] == "2025-02-01T08:00:00+00:00"
        assert len(data["processed_stories"]) == 1
        assert data["processed_stories"][0]["story_id"] == 12345

    def test_creates_directory_if_missing(self, tmp_path: Path) -> None:
        nested_dir = tmp_path / "nested" / "state"
        ts = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        save_last_run_timestamp(nested_dir, ts, "sc")

        state_file = nested_dir / "state.json"
        assert state_file.exists()

    def test_round_trip(self, state_dir: Path) -> None:
        original_ts = datetime(2025, 3, 15, 14, 30, 45, tzinfo=timezone.utc)
        save_last_run_timestamp(state_dir, original_ts, "sc")
        loaded_ts = load_last_run_timestamp(state_dir, "sc")
        assert loaded_ts == original_ts


class TestRecordProcessedStory:
    """Tests for record_processed_story."""

    def test_appends_to_empty_state(self, state_dir: Path) -> None:
        record = ProcessingRecord(
            story_id=99999,
            branch="shortcut/99999",
            status="completed",
            processed_at=datetime(2025, 1, 20, 15, 0, 0, tzinfo=timezone.utc),
        )
        record_processed_story(state_dir, record)

        state_file = state_dir / "state.json"
        data = json.loads(state_file.read_text())
        assert len(data["processed_stories"]) == 1
        assert data["processed_stories"][0]["story_id"] == 99999
        assert data["processed_stories"][0]["branch"] == "shortcut/99999"
        assert data["processed_stories"][0]["status"] == "completed"
        assert (
            data["processed_stories"][0]["processed_at"] == "2025-01-20T15:00:00+00:00"
        )

    def test_appends_to_existing_stories(
        self, state_dir: Path, valid_state_file: Path
    ) -> None:
        record = ProcessingRecord(
            story_id=67890,
            branch="shortcut/67890",
            status="failed",
            processed_at=datetime(2025, 1, 16, 9, 0, 0, tzinfo=timezone.utc),
            failure_reason="git clone failed",
        )
        record_processed_story(state_dir, record)

        data = json.loads(valid_state_file.read_text())
        assert len(data["processed_stories"]) == 2
        assert data["processed_stories"][1]["story_id"] == 67890
        assert data["processed_stories"][1]["failure_reason"] == "git clone failed"

    def test_preserves_last_run_timestamp(
        self, state_dir: Path, valid_state_file: Path
    ) -> None:
        record = ProcessingRecord(
            story_id=11111,
            branch="shortcut/11111",
            status="completed",
            processed_at=datetime(2025, 1, 20, 12, 0, 0, tzinfo=timezone.utc),
        )
        record_processed_story(state_dir, record)

        data = json.loads(valid_state_file.read_text())
        assert data["last_run_timestamps"]["sc"] == "2025-01-15T10:30:00+00:00"

    def test_omits_failure_reason_when_none(self, state_dir: Path) -> None:
        record = ProcessingRecord(
            story_id=22222,
            branch="shortcut/22222",
            status="completed",
            processed_at=datetime(2025, 1, 20, 12, 0, 0, tzinfo=timezone.utc),
            failure_reason=None,
        )
        record_processed_story(state_dir, record)

        state_file = state_dir / "state.json"
        data = json.loads(state_file.read_text())
        assert "failure_reason" not in data["processed_stories"][0]

    def test_includes_failure_reason_when_present(self, state_dir: Path) -> None:
        record = ProcessingRecord(
            story_id=33333,
            branch="shortcut/33333",
            status="failed",
            processed_at=datetime(2025, 1, 20, 12, 0, 0, tzinfo=timezone.utc),
            failure_reason="uv sync failed",
        )
        record_processed_story(state_dir, record)

        state_file = state_dir / "state.json"
        data = json.loads(state_file.read_text())
        assert data["processed_stories"][0]["failure_reason"] == "uv sync failed"

    def test_handles_corrupted_processed_stories_field(self, state_dir: Path) -> None:
        state_file = state_dir / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "last_run_timestamps": {"sc": "2025-01-15T10:30:00+00:00"},
                    "processed_stories": "not a list",
                }
            )
        )
        record = ProcessingRecord(
            story_id=44444,
            branch="shortcut/44444",
            status="completed",
            processed_at=datetime(2025, 1, 20, 12, 0, 0, tzinfo=timezone.utc),
        )
        record_processed_story(state_dir, record)

        data = json.loads(state_file.read_text())
        assert isinstance(data["processed_stories"], list)
        assert len(data["processed_stories"]) == 1
        assert data["processed_stories"][0]["story_id"] == 44444


def _in_flight(state_dir: Path, story_id: str, processed_at: datetime) -> None:
    record_processed_story(
        state_dir,
        ProcessingRecord(
            story_id=story_id,
            branch=story_id,
            status="in_flight",
            processed_at=processed_at,
        ),
    )


class TestReapStaleInFlight:
    """Startup reaper: crashed-mid-session in_flight entries become failed."""

    def test_dead_session_is_reaped(self, state_dir: Path) -> None:
        _in_flight(state_dir, "sc-1", datetime.now(timezone.utc))
        reaped = reap_stale_in_flight(state_dir, is_alive=lambda sid: False)
        assert reaped == ["sc-1"]
        data = json.loads((state_dir / "state.json").read_text())
        entry = data["processed_stories"][0]
        assert entry["status"] == "failed"
        assert "reaped" in entry["failure_reason"]

    def test_live_session_is_kept(self, state_dir: Path) -> None:
        _in_flight(state_dir, "sc-2", datetime.now(timezone.utc))
        reaped = reap_stale_in_flight(state_dir, is_alive=lambda sid: True)
        assert reaped == []
        data = json.loads((state_dir / "state.json").read_text())
        assert data["processed_stories"][0]["status"] == "in_flight"

    def test_unknown_liveness_uses_conservative_age_threshold(
        self, state_dir: Path
    ) -> None:
        now = datetime.now(timezone.utc)
        _in_flight(state_dir, "sc-young", now)
        _in_flight(state_dir, "sc-old", datetime(2025, 1, 1, tzinfo=timezone.utc))
        # Liveness unprobeable (e.g. no tmux): only entries older than the
        # threshold are reaped.
        reaped = reap_stale_in_flight(state_dir, is_alive=lambda sid: None)
        assert reaped == ["sc-old"]
        data = json.loads((state_dir / "state.json").read_text())
        by_id = {e["story_id"]: e["status"] for e in data["processed_stories"]}
        assert by_id == {"sc-young": "in_flight", "sc-old": "failed"}

    def test_terminal_entries_untouched(self, state_dir: Path) -> None:
        record_processed_story(
            state_dir,
            ProcessingRecord(
                story_id="sc-3",
                branch="sc-3",
                status="completed",
                processed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
        )
        assert reap_stale_in_flight(state_dir, is_alive=lambda sid: False) == []


class TestPendingStories:
    """Enqueue-time crash-recovery markers."""

    def test_record_and_load_round_trip(self, state_dir: Path) -> None:
        record_pending_story(state_dir, "sc-5", 5, "src-a")
        pending = load_pending_stories(state_dir)
        assert len(pending) == 1
        assert pending[0]["story_id"] == "sc-5"
        assert pending[0]["ticket_id"] == "5"
        assert pending[0]["source_key"] == "src-a"

    def test_record_is_idempotent_per_story(self, state_dir: Path) -> None:
        record_pending_story(state_dir, "sc-5", 5, None)
        record_pending_story(state_dir, "sc-5", 5, None)
        assert len(load_pending_stories(state_dir)) == 1

    def test_remove_pending_story(self, state_dir: Path) -> None:
        record_pending_story(state_dir, "sc-5", 5, None)
        record_pending_story(state_dir, "sc-6", 6, None)
        remove_pending_story(state_dir, "sc-5")
        assert [e["story_id"] for e in load_pending_stories(state_dir)] == ["sc-6"]

    def test_remove_missing_is_noop(self, state_dir: Path) -> None:
        remove_pending_story(state_dir, "sc-404")
        assert load_pending_stories(state_dir) == []


class TestPrAttempts:
    """Retry-cap bookkeeping for failing PR provisioning."""

    def test_attempts_increment(self, state_dir: Path) -> None:
        assert record_pr_attempt(state_dir, "org/a", 7) == 1
        assert record_pr_attempt(state_dir, "org/a", 7) == 2
        # A different PR (or repo) counts separately.
        assert record_pr_attempt(state_dir, "org/b", 7) == 1

    def test_clear_resets_count(self, state_dir: Path) -> None:
        record_pr_attempt(state_dir, "org/a", 7)
        record_pr_attempt(state_dir, "org/a", 7)
        clear_pr_attempts(state_dir, "org/a", 7)
        assert record_pr_attempt(state_dir, "org/a", 7) == 1
