# Feature: ticket-ingestion-pipeline, Property 4: Timestamp persistence round-trip
"""Property-based test for timestamp persistence round-trip.

**Validates: Requirements 3.2**

Property: For any valid datetime timestamp, writing it to the state file
and reading it back SHALL produce a timestamp equal to the original.
"""

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis.strategies import datetimes

from backend.ticket_ingestion.state import (
    load_last_run_timestamp,
    save_last_run_timestamp,
)

# Generate timezone-aware UTC datetimes.
# We generate naive datetimes and attach UTC timezone to ensure all generated
# values are valid UTC timestamps that can round-trip through ISO 8601 format.
utc_datetimes = datetimes(
    min_value=datetime(1970, 1, 1),
    max_value=datetime(9999, 12, 31),
).map(lambda dt: dt.replace(tzinfo=timezone.utc))


@settings(max_examples=100, deadline=None)
@given(ts=utc_datetimes)
def test_timestamp_persistence_round_trip(ts: datetime, tmp_path_factory) -> None:
    """For any valid datetime timestamp, writing it to the state file and
    reading it back SHALL produce a timestamp equal to the original."""
    state_dir = tmp_path_factory.mktemp("state")

    save_last_run_timestamp(state_dir, ts, "sc")
    loaded_ts = load_last_run_timestamp(state_dir, "sc")

    assert (
        loaded_ts == ts
    ), f"Round-trip failed: wrote {ts.isoformat()} but read {loaded_ts.isoformat()}"
