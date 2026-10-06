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


# --------------------------------------------------------------------------- #
# start_comment: announcing the move on the ticket
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _forget_names():
    start_state._NAMES.clear()
    yield
    start_state._NAMES.clear()


def _commenting_provider(name="Ethan Mandel"):
    provider = AsyncMock()
    provider.test_connection.return_value = ({"member_id": "m", "name": name}, "")
    return provider


@pytest.mark.asyncio
async def test_the_move_is_announced_when_the_source_asks():
    story = make_ticket(id=123)
    story.source_key = "sc-main"
    provider = _commenting_provider()
    with _on_disk(_config(_source(start_state="500", start_comment=True))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await start_state.move_started(story) == "500"
    provider.add_comment.assert_awaited_once_with(
        "123", "MindFlock (Ethan) is taking this on."
    )


@pytest.mark.asyncio
async def test_no_comment_unless_the_source_asks():
    # Off is the default: every source that existed before the toggle moves
    # tickets exactly as silently as it used to.
    story = make_ticket()
    story.source_key = "sc-main"
    provider = _commenting_provider()
    with _on_disk(_config(_source(start_state="500"))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await start_state.move_started(story) == "500"
    provider.add_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_comment_without_a_move():
    # The comment announces the move; with no start state there is nothing to
    # announce, toggle or not.
    story = make_ticket()
    story.source_key = "sc-main"
    provider = _commenting_provider()
    with _on_disk(_config(_source(start_state="", start_comment=True))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await start_state.move_started(story) == ""
    provider.add_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_move_is_not_announced():
    story = make_ticket()
    story.source_key = "sc-main"
    provider = _commenting_provider()
    provider.set_state.side_effect = RuntimeError("Jira said no")
    with _on_disk(_config(_source(start_state="500", start_comment=True))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await start_state.move_started(story) == ""
    provider.add_comment.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("state_id", "500"), ("state", "In Development")]
)
async def test_a_ticket_already_in_the_start_state_is_not_announced_again(field, value):
    # Relaunching a ticket that is already in development must not stack a
    # second "taking this on" under the first. Jira configs may name the status.
    story = make_ticket()
    story.source_key = "sc-main"
    setattr(story, field, value)
    target = "500" if field == "state_id" else "in development"
    provider = _commenting_provider()
    with _on_disk(_config(_source(start_state=target, start_comment=True))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await start_state.move_started(story) == target
    provider.add_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_comment_warns_and_keeps_the_move(caplog):
    story = make_ticket()
    story.source_key = "sc-main"
    provider = _commenting_provider()
    provider.add_comment.side_effect = RuntimeError("comments are locked")
    with _on_disk(_config(_source(start_state="500", start_comment=True))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            with caplog.at_level(logging.WARNING):
                assert await start_state.move_started(story) == "500"
    assert "comments are locked" in caplog.text


@pytest.mark.asyncio
async def test_a_nameless_account_still_gets_a_clean_comment():
    story = make_ticket(id=9)
    story.source_key = "sc-main"
    provider = AsyncMock()
    provider.test_connection.return_value = (None, "HTTP 500")
    with _on_disk(_config(_source(start_state="500", start_comment=True))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            await start_state.move_started(story)
    provider.add_comment.assert_awaited_once_with("9", "MindFlock is taking this on.")


@pytest.mark.asyncio
async def test_the_name_is_looked_up_once_per_account():
    provider = _commenting_provider()
    provider.test_connection.return_value = (
        {"name": "ethan", "display_name": "Ethan Mandel"},
        "",
    )
    with _on_disk(_config(_source(start_state="500", start_comment=True))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            for n in (1, 2):
                story = make_ticket(id=n)
                story.source_key = "sc-main"
                await start_state.move_started(story)
    assert provider.test_connection.await_count == 1
    # display_name (Shortcut's real name) beats name (its @mention handle).
    provider.add_comment.assert_awaited_with(
        "2", "MindFlock (Ethan) is taking this on."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [None, {}])
async def test_a_failed_name_lookup_is_retried_on_the_next_ticket(identity):
    # Only a successful lookup is remembered: a flaky identity call costs one
    # nameless comment, not a nameless comment for the rest of the process.
    provider = AsyncMock()
    provider.test_connection.return_value = (identity, "HTTP 500")
    with _on_disk(_config(_source(start_state="500", start_comment=True))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            for n in (1, 2):
                story = make_ticket(id=n)
                story.source_key = "sc-main"
                assert await start_state.move_started(story) == "500"
    provider.add_comment.assert_awaited_with("2", "MindFlock is taking this on.")
    assert provider.test_connection.await_count == 2
    assert start_state._NAMES == {}


@pytest.mark.asyncio
async def test_a_name_lookup_that_raises_warns_once_and_keeps_the_move(caplog):
    story = make_ticket()
    story.source_key = "sc-main"
    provider = AsyncMock()
    provider.test_connection.side_effect = RuntimeError("identity endpoint down")
    with _on_disk(_config(_source(start_state="500", start_comment=True))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            with caplog.at_level(logging.WARNING):
                assert await start_state.move_started(story) == "500"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "identity endpoint down" in warnings[0].getMessage()
    provider.add_comment.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second",
    [
        {"api_token": "t2"},  # a second account on the same tracker
        {"base_url": "https://two.atlassian.net"},  # a second Jira site
    ],
)
async def test_each_account_is_named_by_its_own_lookup(second):
    jira = dict(provider="jira", email="e@x", base_url="https://one.atlassian.net")
    one = _source(id="j1", start_state="3", start_comment=True, **jira)
    two = _source(id="j2", start_state="3", start_comment=True, **{**jira, **second})
    by_source = {
        "j1": _commenting_provider("Ann One"),
        "j2": _commenting_provider("Bo Two"),
    }
    with _on_disk(_config(one, two)):
        with patch(
            "backend.ticket_ingestion.providers.get_provider",
            side_effect=lambda cfg: by_source[cfg.id],
        ):
            for key in ("j1", "j2"):
                story = make_ticket(id=key)
                story.source_key = key
                await start_state.move_started(story)
    for key, who in (("j1", "Ann"), ("j2", "Bo")):
        by_source[key].test_connection.assert_awaited_once()
        by_source[key].add_comment.assert_awaited_once_with(
            key, f"MindFlock ({who}) is taking this on."
        )


@pytest.mark.asyncio
async def test_already_in_matches_the_state_name_loosely():
    # A hand-edited Jira config names the status; the tracker's casing and
    # padding don't make a relaunch look like a fresh move.
    story = make_ticket()
    story.source_key = "jira"
    story.state = "in progress "
    provider = _commenting_provider()
    src = _source(
        provider="jira",
        id="jira",
        email="e@x",
        base_url="https://x.atlassian.net",
        start_state="In Progress",
        start_comment=True,
    )
    with _on_disk(_config(src)):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await start_state.move_started(story) == "In Progress"
    provider.set_state.assert_awaited_once()
    provider.add_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_ticket_that_reports_no_state_is_announced():
    # Announcing once too often beats never announcing.
    story = make_ticket(id=5)
    story.source_key = "sc-main"
    assert story.state == "" and story.state_id == ""
    provider = _commenting_provider()
    with _on_disk(_config(_source(start_state="500", start_comment=True))):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            await start_state.move_started(story)
    provider.add_comment.assert_awaited_once_with(
        "5", "MindFlock (Ethan) is taking this on."
    )


@pytest.mark.asyncio
async def test_the_force_start_path_announces_the_move_too(monkeypatch):
    # The web force-start goes through the same mover, so a ticket started by
    # hand from the panel says so on the ticket exactly like a pipeline one.
    from backend.web.core import ticket_start

    cfg = _config(_source(start_state="500", start_comment=True))
    monkeypatch.setattr(ticket_start, "_load_config", lambda: cfg)
    story = make_ticket(id=77)
    story.source_key = "sc-main"
    provider = _commenting_provider()
    with _on_disk(cfg):
        with patch(
            "backend.ticket_ingestion.providers.get_provider", return_value=provider
        ):
            assert await ticket_start.move_to_start_state(story) == "500"
    provider.set_state.assert_awaited_once_with("77", "500")
    provider.add_comment.assert_awaited_once_with(
        "77", "MindFlock (Ethan) is taking this on."
    )
