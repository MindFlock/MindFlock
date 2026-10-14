"""Intake round 4: a new ticket source auto-starts nothing until a state (or
label) is picked, a state typed before Test never widens the search, and the
"Pause intake" record lives in the settings store.

Existing sources keep their behaviour: ``needs_filter`` is only written on
sources Intake creates, and ``general.intake_paused`` defaults to "not paused".
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.config.settings import GeneralSettings, TicketingSource
from backend.ticket_ingestion.backfill import BackfillScanner
from backend.ticket_ingestion.config import (
    PipelineConfig,
    TicketProviderConfig,
    _parse_source,
)
from backend.ticket_ingestion.providers.base import (
    FILTER_UNMATCHED,
    NO_FILTER_YET,
    auto_start_waiting,
)
from tests._factories import make_ticket


def _src(**kw) -> TicketProviderConfig:
    base = {"provider": "shortcut", "api_token": "t", "member_id": "m"}
    base.update(kw)
    return TicketProviderConfig(**base)


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def test_a_new_source_waits_for_a_state():
    assert auto_start_waiting(_src(needs_filter=True)) == NO_FILTER_YET


def test_a_state_or_a_label_opens_it():
    assert auto_start_waiting(_src(needs_filter=True, workflow_state="500")) == ""
    assert auto_start_waiting(_src(needs_filter=True, ingest_labels="mindflock")) == ""
    # The legacy integer field counts as a state.
    assert auto_start_waiting(_src(needs_filter=True, workflow_state_id=7)) == ""


def test_an_older_source_keeps_every_state():
    """Never flip an existing setup: no flag = the historic empty filter."""
    assert auto_start_waiting(_src()) == ""


def test_a_typed_name_never_widens_the_search():
    # Shortcut drops a non-numeric state, so a typed name alone would read
    # "every state" — the gate holds it until Test swaps in the id.
    assert auto_start_waiting(_src(workflow_state="Ready for dev")) == FILTER_UNMATCHED
    assert auto_start_waiting(_src(workflow_state="Ready for dev,500")) == ""
    assert (
        auto_start_waiting(_src(provider="linear", workflow_state="Todo"))
        == FILTER_UNMATCHED
    )
    assert (
        auto_start_waiting(
            _src(
                provider="linear", workflow_state="0b5f6a1e-1c2d-4e3f-8a9b-0c1d2e3f4a5b"
            )
        )
        == ""
    )
    # Jira filters by status name natively.
    assert auto_start_waiting(_src(provider="jira", workflow_state="To Do")) == ""


def test_github_issues_is_bounded_by_its_repo_not_a_state():
    assert auto_start_waiting(_src(provider="github_issues", needs_filter=True)) == ""


# --------------------------------------------------------------------------- #
# Storage: settings.json → pipeline config
# --------------------------------------------------------------------------- #
def test_needs_filter_round_trips_and_reaches_the_pipeline():
    s = TicketingSource.from_dict(
        {"id": "sc", "provider": "shortcut", "needs_filter": "on"}
    )
    assert s.needs_filter == "on"
    d = s.to_dict()
    assert d["needs_filter"] == "on"
    cfg, _problems = _parse_source(d, Path("config.toml"), 0)
    assert cfg.needs_filter is True
    # Absent stays absent: an old source's stored shape is unchanged.
    old = TicketingSource.from_dict({"id": "sc", "provider": "shortcut"})
    assert "needs_filter" not in old.to_dict()
    assert _parse_source(old.to_dict(), Path("config.toml"), 0)[0].needs_filter is False


def test_intake_paused_defaults_to_not_paused():
    g = GeneralSettings.from_dict({})
    assert g.intake_paused is None
    assert "intake_paused" not in g.to_dict()


def test_intake_paused_keeps_known_kinds_in_tab_order():
    g = GeneralSettings.from_dict(
        {"intake_paused": ["issues", "bogus", "tickets", "tickets"]}
    )
    assert g.intake_paused == ["tickets", "issues"]
    assert g.to_dict()["intake_paused"] == ["tickets", "issues"]
    # An empty record is still "paused" (Resume turns on what is set up).
    assert GeneralSettings.from_dict({"intake_paused": []}).intake_paused == []
    assert GeneralSettings.from_dict({"intake_paused": "tickets"}).intake_paused is None


# --------------------------------------------------------------------------- #
# The scanner honours the gate
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    from backend.ticket_ingestion import backfill as backfill_mod

    monkeypatch.setattr(backfill_mod, "_STATE_DIR", tmp_path)
    return tmp_path


def _config(source: TicketProviderConfig) -> PipelineConfig:
    return PipelineConfig(
        ticketing=source,
        repo_url="git@github.com:org/repo.git",
        workspace_dir=Path("/tmp/workspaces"),
        min_description_length=20,
        log_file=Path("/tmp/pipeline.log"),
        log_level="INFO",
    )


async def test_scan_fetches_nothing_and_keeps_the_checkpoint_while_waiting():
    source = _src(needs_filter=True)
    queue: asyncio.Queue = asyncio.Queue()
    scanner = BackfillScanner(_config(source), queue, source)
    with (
        patch("backend.ticket_ingestion.backfill.save_last_run_timestamp") as save,
        patch.object(scanner, "_fetch_stories", new_callable=AsyncMock) as fetch,
    ):
        assert await scanner.scan() == 0
    fetch.assert_not_called()
    save.assert_not_called()
    assert queue.empty()


async def test_scan_runs_once_a_state_is_picked():
    source = _src(needs_filter=True, workflow_state="500")
    queue: asyncio.Queue = asyncio.Queue()
    scanner = BackfillScanner(_config(source), queue, source)
    story = make_ticket(
        id=1,
        name="Story 1",
        description="Test description with enough length",
        acceptance_criteria=["c"],
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    with (
        patch(
            "backend.ticket_ingestion.backfill.load_last_run_timestamp",
            return_value=datetime(2025, 1, 1, tzinfo=timezone.utc),
        ),
        patch("backend.ticket_ingestion.backfill.save_last_run_timestamp"),
        patch.object(scanner, "_fetch_stories", new_callable=AsyncMock) as fetch,
        patch(
            "backend.ticket_ingestion.backfill._get_existing_branches",
            new_callable=AsyncMock,
            return_value=set(),
        ),
    ):
        fetch.return_value = [story]
        assert await scanner.scan() == 1
