"""Moving a ticket into its source's state when a session starts for it.

Covers :mod:`backend.ticket_ingestion.start_state` — the one place both launch
paths (the pipeline's ``SessionRunner`` and the web force-start) go through — and
the two properties that matter about it:

* it resolves the source from the config **on disk now**, keyed by the source
  the ticket actually came from (not its provider name, which two sources of the
  same provider share), and
* it never raises. The session is the work; the board is bookkeeping about it.

No network: the provider adapter is stubbed at the registry boundary.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion import start_state
from tests._factories import make_ticket


def _config(*sources) -> PipelineConfig:
    return PipelineConfig(
        repo_url="git@github.com:o/r.git",
        workspace_dir=Path("/tmp/ws"),
        log_file=Path("/tmp/l.log"),
        log_level="INFO",
        poll_interval_seconds=20,
        ticketing_sources=list(sources),
    )


def _source(**over) -> TicketProviderConfig:
    base = dict(
        provider="shortcut", id="sc-main", api_token="t", member_id="m", start_state="7"
    )
    base.update(over)
    return TicketProviderConfig(**base)


def _on_disk(cfg):
    """Patch the fresh-config read every resolver here goes through."""
    return patch("backend.ticket_ingestion.config.config_for_launch", return_value=cfg)


# --------------------------------------------------------------------------- #
# Source resolution
# --------------------------------------------------------------------------- #
def test_source_key_prefers_the_stamp_over_the_provider_name():
    story = make_ticket(provider="jira")
    story.source_key = "jira-eu"
    assert start_state.source_key_of(story) == "jira-eu"
    # An unstamped ticket (built by hand, or by an older pipeline) still
    # resolves an unkeyed source, which matches on provider.
    story.source_key = ""
    assert start_state.source_key_of(story) == "jira"


def test_target_reads_the_config_on_disk_not_the_snapshot():
    story = make_ticket()
    story.source_key = "sc-main"
    stale = _config(_source(start_state="1"))
    fresh = _config(_source(start_state="9"))
    with _on_disk(fresh):
        src, target = start_state.target_for(story, stale)
    assert target == "9"
    assert src.id == "sc-main"


def test_target_falls_back_to_the_snapshot_when_disk_is_unreadable():
    story = make_ticket()
    story.source_key = "sc-main"
    snapshot = _config(_source(start_state="4"))
    with _on_disk(None):
        _, target = start_state.target_for(story, snapshot)
    assert target == "4"


def test_no_target_when_the_source_is_gone_or_unset():
    story = make_ticket()
    story.source_key = "sc-main"
    with _on_disk(_config(_source(id="other"))):
        assert start_state.target_for(story, None) == (None, "")
    with _on_disk(_config(_source(start_state=""))):
        assert start_state.target_for(story, None)[1] == ""


# --------------------------------------------------------------------------- #
# The move itself
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_move_calls_the_provider_with_the_native_ticket_id():
    story = make_ticket(id=123)
    story.source_key = "sc-main"
    provider = AsyncMock()
    with _on_disk(_config(_source(start_state="500"))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            moved = await start_state.move_started(story)
    assert moved == "500"
    provider.set_state.assert_awaited_once_with("123", "500")


@pytest.mark.asyncio
async def test_move_is_a_no_op_when_no_start_state_is_configured():
    story = make_ticket()
    story.source_key = "sc-main"
    provider = AsyncMock()
    with _on_disk(_config(_source(start_state=""))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await start_state.move_started(story) == ""
    provider.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_move_warns_instead_of_raising(caplog):
    # The session is already live by the time this runs; a tracker that refuses
    # the move must not turn a working session into a failed launch.
    story = make_ticket()
    story.source_key = "sc-main"
    provider = AsyncMock()
    provider.set_state.side_effect = RuntimeError("Jira said no")
    with _on_disk(_config(_source(start_state="500"))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            with caplog.at_level(logging.WARNING):
                assert await start_state.move_started(story) == ""
    assert "Jira said no" in caplog.text


@pytest.mark.asyncio
async def test_an_unreadable_config_is_not_a_launch_error():
    story = make_ticket()
    story.source_key = "sc-main"
    with patch(
        "backend.ticket_ingestion.config.config_for_launch",
        side_effect=RuntimeError("config.toml is gone"),
    ):
        # Not an exception, not a half-launched session: nothing to move.
        assert start_state.target_for(story, None) == (None, "")
        assert await start_state.move_started(story) == ""


@pytest.mark.asyncio
async def test_a_provider_that_cannot_even_be_BUILT_is_swallowed(caplog):
    """The failure that is not inside ``set_state``.

    A source whose credentials were cleared (or whose provider id was hand-
    edited into something unregistered) blows up in ``get_provider`` — before
    any coroutine exists to await. That is a launch-time error like any other
    here: the session is already live, so it costs a warning.
    """
    story = make_ticket()
    story.source_key = "sc-main"
    with _on_disk(_config(_source(start_state="500"))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider",
            side_effect=ValueError("unknown provider 'shortcutt'"),
        ):
            with caplog.at_level(logging.WARNING):
                assert await start_state.move_started(story) == ""
    assert "unknown provider" in caplog.text


@pytest.mark.asyncio
async def test_a_start_state_cleared_since_ingest_moves_nothing():
    """The disk read, not the snapshot, decides.

    A ticket can sit in the queue across a config change, and the answer to
    "where does started work go" is the one the owner is looking at in the UI
    right now — including when the answer is "nowhere, I turned that off".
    """
    story = make_ticket()
    story.source_key = "sc-main"
    at_ingest = _config(_source(start_state="500"))
    now = _config(_source(start_state=""))
    provider = AsyncMock()
    with _on_disk(now):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await start_state.move_started(story, at_ingest) == ""
    provider.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_provider_that_cannot_move_a_ticket_is_never_asked_to():
    """A ``start_state`` left behind by a provider switch (Jira -> GitHub
    Issues) is inert rather than an error on every single launch — the same
    fail-narrow shape ``start_state_id`` gives the parser."""
    story = make_ticket()
    story.source_key = "gh"
    provider = AsyncMock()
    with _on_disk(
        _config(_source(provider="github_issues", id="gh", start_state="Doing"))
    ):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await start_state.move_started(story) == ""
    provider.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_move_happens_once_and_reports_what_it_did():
    """The caller logs (pipeline) or surfaces (Intake) the return value, so
    "nothing to do" and "moved into 500" have to be distinguishable — and a
    single launch may only write to the tracker once."""
    story = make_ticket(id=77)
    story.source_key = "sc-main"
    provider = AsyncMock()
    with _on_disk(_config(_source(start_state="500"))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await start_state.move_started(story) == "500"
    assert provider.set_state.await_count == 1
