"""A queued ticket is re-checked against its source's ingest filters before it
launches — at the crash-recovery re-enqueue and again at dequeue.

The scan applies the filters server-side, but a ticket can wait for days after
that: behind the session cap, and across restarts via its ``pending`` marker. A
burst of 129 tickets queued that way kept relaunching after they had been
triaged back out of "Will do", and each launch moved its ticket into the start
state, undoing the triage. These pin the drop, and that a ticket that still
matches (or can't be re-read) launches exactly as before.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from backend.ticket_ingestion import orchestrator as orchestrator_mod
from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion.models import (
    ProcessingRecord,
    ProvisionedEnvironment,
    Ticket,
    WebhookEvent,
)
from backend.ticket_ingestion.orchestrator import PipelineOrchestrator
from backend.ticket_ingestion.providers import TicketNotFound
from backend.ticket_ingestion.providers.base import ProviderError, ingest_filter_miss
from backend.ticket_ingestion.providers.shortcut import (
    ShortcutProvider,
    story_from_api_response,
)
from backend.ticket_ingestion.state import (
    _read_state,
    load_pending_stories,
    record_pending_story,
    record_processed_story,
)
from tests._factories import make_ticket

WILL_DO = "500000007"
UNSCHEDULED = "500000006"
_ORCH_LOGGER = "backend.ticket_ingestion.orchestrator"


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    state_dir = tmp_path / "ledger"
    state_dir.mkdir()
    monkeypatch.setattr(orchestrator_mod, "_STATE_DIR", state_dir)
    return state_dir


def _source(**overrides) -> TicketProviderConfig:
    fields = dict(
        provider="shortcut",
        id="sc",
        api_token="t",
        member_id="member-123",
        workflow_state=WILL_DO,
    )
    fields.update(overrides)
    return TicketProviderConfig(**fields)


def _orchestrator(tmp_path, source=None, fetched=None) -> PipelineOrchestrator:
    source = source or _source()
    config = PipelineConfig(
        ticketing=source,
        ticketing_sources=[source],
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "workspaces",
        min_description_length=20,
        log_file=tmp_path / "pipeline.log",
    )
    orch = PipelineOrchestrator(config)
    orch._scanners[0]._provider = MagicMock()
    orch._scanners[0]._provider.fetch = AsyncMock(
        side_effect=fetched if isinstance(fetched, Exception) else None,
        return_value=fetched,
    )
    return orch


# --------------------------------------------------------------------------- #
# The filter itself
# --------------------------------------------------------------------------- #
class TestIngestFilterMiss:
    def test_a_ticket_still_in_the_ingest_state_passes(self):
        assert ingest_filter_miss(_source(), make_ticket(state_id=WILL_DO)) == ""

    def test_a_ticket_moved_out_of_the_ingest_state_fails_and_says_where(self):
        ticket = make_ticket(state_id=UNSCHEDULED, state="Unscheduled")
        assert "Unscheduled" in ingest_filter_miss(_source(), ticket)

    def test_any_of_several_ingest_states_passes(self):
        cfg = _source(workflow_state=f"{UNSCHEDULED},{WILL_DO}")
        assert ingest_filter_miss(cfg, make_ticket(state_id=UNSCHEDULED)) == ""

    def test_an_unknown_state_passes(self):
        """A provider that didn't report a state can't prove the ticket left
        it, and the scan already vetted it."""
        assert ingest_filter_miss(_source(), make_ticket()) == ""

    def test_no_ingest_state_configured_passes_anything(self):
        cfg = _source(workflow_state="")
        assert ingest_filter_miss(cfg, make_ticket(state_id=UNSCHEDULED)) == ""

    def test_jira_matches_a_by_name_filter_on_the_state_name(self):
        cfg = _source(provider="jira", workflow_state="Ready for Dev")
        assert ingest_filter_miss(cfg, make_ticket(state="ready for dev")) == ""
        assert ingest_filter_miss(cfg, make_ticket(state="In Progress")) != ""

    def test_a_stale_state_on_a_stateless_provider_is_ignored(self):
        """GitHub Issues has no workflow states, so a ``workflow_state`` left
        in a hand-edited config can't drop anything."""
        cfg = _source(provider="github_issues")
        assert ingest_filter_miss(cfg, make_ticket(state="open")) == ""

    def test_a_ticket_that_lost_its_ingest_label_fails(self):
        cfg = _source(workflow_state="", ingest_labels="mindflock")
        assert ingest_filter_miss(cfg, make_ticket(labels=["MindFlock"])) == ""
        assert "label" in ingest_filter_miss(cfg, make_ticket(labels=["other"]))

    def test_a_legacy_workflow_state_id_config_is_re_checked_too(self):
        """The scan honours the legacy integer ``workflow_state_id`` when
        ``workflow_state`` is empty, so a source configured that way must not
        slip past the re-check — that was the very bug, just keyed differently."""
        cfg = _source(workflow_state="", workflow_state_id=int(WILL_DO))
        assert ingest_filter_miss(cfg, make_ticket(state_id=WILL_DO)) == ""
        assert ingest_filter_miss(cfg, make_ticket(state_id=UNSCHEDULED)) != ""

    def test_the_legacy_key_loses_to_workflow_state(self):
        cfg = _source(workflow_state=UNSCHEDULED, workflow_state_id=int(WILL_DO))
        assert ingest_filter_miss(cfg, make_ticket(state_id=UNSCHEDULED)) == ""

    def test_the_legacy_key_is_shortcut_only(self):
        cfg = _source(provider="jira", workflow_state="", workflow_state_id=1)
        assert ingest_filter_miss(cfg, make_ticket(state_id="2", state="Done")) == ""

    def test_jira_state_names_match_case_insensitively(self):
        cfg = _source(provider="jira", workflow_state="In Progress")
        assert ingest_filter_miss(cfg, make_ticket(state="in progress")) == ""

    def test_linear_state_ids_match_case_insensitively(self):
        cfg = _source(provider="linear", workflow_state="AB12-CD34")
        assert ingest_filter_miss(cfg, make_ticket(state_id="ab12-cd34")) == ""

    def test_a_multi_state_config_with_spaces_matches_a_later_entry(self):
        cfg = _source(workflow_state=f"{WILL_DO}, 500000008")
        assert ingest_filter_miss(cfg, make_ticket(state_id="500000008")) == ""

    def test_right_state_but_missing_label_reports_the_label(self):
        cfg = _source(ingest_labels="mindflock")
        miss = ingest_filter_miss(cfg, make_ticket(state_id=WILL_DO, labels=["x"]))
        assert "label" in miss

    def test_the_state_reason_wins_when_both_filters_miss(self):
        cfg = _source(ingest_labels="mindflock")
        miss = ingest_filter_miss(
            cfg, make_ticket(state_id=UNSCHEDULED, state="Unscheduled", labels=[])
        )
        assert "ingest state" in miss and "label" not in miss

    def test_the_reason_names_the_state_id_when_the_name_is_unknown(self):
        """Shortcut tickets often carry only the id; the log line must still
        say where the ticket went."""
        miss = ingest_filter_miss(_source(), make_ticket(state_id=UNSCHEDULED))
        assert f"now in {UNSCHEDULED}" in miss

    @pytest.mark.parametrize("provider", ["jira", "linear"])
    def test_ingest_labels_on_a_non_shortcut_source_are_ignored(self, provider):
        cfg = _source(provider=provider, workflow_state="", ingest_labels="mindflock")
        assert ingest_filter_miss(cfg, make_ticket(labels=[])) == ""

    @pytest.mark.parametrize(
        "cfg",
        [SimpleNamespace(workflow_state=WILL_DO), SimpleNamespace(provider=None)],
    )
    def test_a_config_without_a_provider_does_not_raise(self, cfg):
        assert ingest_filter_miss(cfg, make_ticket(state_id=UNSCHEDULED)) == ""

    def test_ticket_not_found_is_still_a_provider_error(self):
        """Existing ``except ProviderError`` / ``except RuntimeError`` callers
        (the webhook fetch, force-start) keep catching it."""
        assert issubclass(TicketNotFound, ProviderError)
        assert issubclass(TicketNotFound, RuntimeError)

    def test_a_ticket_has_no_state_id_by_default(self):
        assert make_ticket().state_id == ""


# --------------------------------------------------------------------------- #
# Which source a queued ticket is checked against
# --------------------------------------------------------------------------- #
def _two_source_orchestrator(tmp_path):
    """Primary ``sc`` ingests from Will Do; secondary ``sc-qa`` from
    Unscheduled. Each scanner gets its own fake provider."""
    primary = _source()
    secondary = _source(id="sc-qa", api_token="t2", workflow_state=UNSCHEDULED)
    config = PipelineConfig(
        ticketing=primary,
        ticketing_sources=[primary, secondary],
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "workspaces",
        min_description_length=20,
        log_file=tmp_path / "pipeline.log",
    )
    orch = PipelineOrchestrator(config)
    for scanner in orch._scanners:
        scanner._provider = MagicMock()
        scanner._provider.fetch = AsyncMock()
    return orch


class TestScannerFor:
    def test_a_known_key_finds_its_own_scanner(self, tmp_path):
        orch = _two_source_orchestrator(tmp_path)
        assert orch._scanner_for("sc-qa")._source_key == "sc-qa"

    @pytest.mark.parametrize("key", [None, "", "removed-from-config"])
    def test_an_unstamped_or_stale_key_falls_back_to_the_primary(self, tmp_path, key):
        orch = _two_source_orchestrator(tmp_path)
        assert orch._scanner_for(key) is orch._scanners[0]


# --------------------------------------------------------------------------- #
# Crash-recovery re-enqueue
# --------------------------------------------------------------------------- #
class TestRequeueRecheck:
    async def test_a_secondary_source_marker_is_checked_against_that_source(
        self, tmp_path, _isolated_state_dir
    ):
        """In Unscheduled is a pass for ``sc-qa`` even though it is a miss
        for the primary — and the re-fetch goes through ``sc-qa``'s provider."""
        orch = _two_source_orchestrator(tmp_path)
        primary, secondary = orch._scanners
        secondary._provider.fetch.return_value = make_ticket(id=7, state_id=UNSCHEDULED)
        record_pending_story(_isolated_state_dir, "sc-7", 7, "sc-qa")

        await orch._requeue_pending_stories()

        primary._provider.fetch.assert_not_awaited()
        secondary._provider.fetch.assert_awaited_once_with("7")
        assert orch._queue.get_nowait().id == 7

    async def test_a_secondary_source_drops_on_its_own_state(
        self, tmp_path, _isolated_state_dir
    ):
        orch = _two_source_orchestrator(tmp_path)
        orch._scanners[1]._provider.fetch.return_value = make_ticket(
            id=7, state_id=WILL_DO
        )
        record_pending_story(_isolated_state_dir, "sc-7", 7, "sc-qa")

        await orch._requeue_pending_stories()

        assert orch._queue.empty()
        assert load_pending_stories(_isolated_state_dir) == []

    async def test_an_already_processed_marker_is_dropped_without_a_fetch(
        self, tmp_path, _isolated_state_dir
    ):
        orch = _orchestrator(tmp_path, fetched=make_ticket(id=7, state_id=WILL_DO))
        record_pending_story(_isolated_state_dir, "sc-7", 7, "sc")
        record_processed_story(
            _isolated_state_dir,
            ProcessingRecord(
                story_id="sc-7",
                branch="sc-7",
                status="completed",
                processed_at=datetime.now(timezone.utc),
            ),
        )

        await orch._requeue_pending_stories()

        orch._scanners[0]._provider.fetch.assert_not_awaited()
        assert orch._queue.empty()
        assert load_pending_stories(_isolated_state_dir) == []

    async def test_a_dropped_ticket_leaves_no_ledger_entry(
        self, tmp_path, _isolated_state_dir, caplog
    ):
        """Nothing in processed_stories, so moving it back into the ingest
        state makes it a fresh scan match."""
        orch = _orchestrator(tmp_path, fetched=make_ticket(id=7, state_id=UNSCHEDULED))
        record_pending_story(_isolated_state_dir, "sc-7", 7, "sc")

        with caplog.at_level(logging.INFO, logger=_ORCH_LOGGER):
            await orch._requeue_pending_stories()

        assert _read_state(_isolated_state_dir).get("processed_stories", []) == []
        assert "Dropped pending ticket sc-7" in caplog.text

    async def test_a_ticket_triaged_out_of_the_ingest_state_loses_its_marker(
        self, tmp_path, _isolated_state_dir
    ):
        orch = _orchestrator(tmp_path, fetched=make_ticket(id=7, state_id=UNSCHEDULED))
        record_pending_story(_isolated_state_dir, "sc-7", 7, "sc")

        await orch._requeue_pending_stories()

        assert orch._queue.empty()
        assert load_pending_stories(_isolated_state_dir) == []

    async def test_a_ticket_still_in_the_ingest_state_is_re_enqueued(
        self, tmp_path, _isolated_state_dir
    ):
        orch = _orchestrator(tmp_path, fetched=make_ticket(id=7, state_id=WILL_DO))
        record_pending_story(_isolated_state_dir, "sc-7", 7, "sc")

        await orch._requeue_pending_stories()

        assert orch._queue.get_nowait().id == 7
        # The marker stays until process_story picks the ticket up.
        assert [e["story_id"] for e in load_pending_stories(_isolated_state_dir)] == [
            "sc-7"
        ]

    async def test_a_deleted_ticket_loses_its_marker(
        self, tmp_path, _isolated_state_dir
    ):
        """It used to be retried, and warned about, on every startup forever."""
        orch = _orchestrator(tmp_path, fetched=TicketNotFound("404"))
        record_pending_story(_isolated_state_dir, "sc-7", 7, "sc")

        await orch._requeue_pending_stories()

        assert orch._queue.empty()
        assert load_pending_stories(_isolated_state_dir) == []

    async def test_an_unreachable_tracker_keeps_the_marker_for_next_time(
        self, tmp_path, _isolated_state_dir
    ):
        orch = _orchestrator(tmp_path, fetched=ProviderError("503"))
        record_pending_story(_isolated_state_dir, "sc-7", 7, "sc")

        await orch._requeue_pending_stories()

        assert orch._queue.empty()
        assert len(load_pending_stories(_isolated_state_dir)) == 1


# --------------------------------------------------------------------------- #
# Dequeue, just before launch
# --------------------------------------------------------------------------- #
def _launchable(orch):
    env = ProvisionedEnvironment(
        directory=Path("/tmp/workspaces/sc-7"), branch_name="sc-7", cursor_window_id=1
    )
    orch._provisioner.provision = AsyncMock(return_value=env)
    orch._claude_runner.invoke = AsyncMock()
    orch._cs_runner = None
    return orch


def _queued(**overrides):
    fields = dict(
        id=7,
        source_key="sc",
        state_id=WILL_DO,
        description="A queued ticket with a long enough description.",
        owner_ids=["member-123"],
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return make_ticket(**fields)


class TestLaunchRecheck:
    async def test_a_ticket_moved_out_while_it_waited_is_not_launched(
        self, tmp_path, _isolated_state_dir
    ):
        orch = _launchable(
            _orchestrator(tmp_path, fetched=_queued(state_id=UNSCHEDULED))
        )

        await orch.process_story(_queued())

        orch._provisioner.provision.assert_not_called()
        # Nothing recorded: moved back into the ingest state, the next poll
        # picks it up again.
        assert _read_state(_isolated_state_dir).get("processed_stories", []) == []

    async def test_a_ticket_deleted_while_it_waited_is_not_launched(self, tmp_path):
        orch = _launchable(_orchestrator(tmp_path, fetched=TicketNotFound("gone")))

        await orch.process_story(_queued())

        orch._provisioner.provision.assert_not_called()

    async def test_a_ticket_still_in_the_ingest_state_launches(self, tmp_path):
        orch = _launchable(_orchestrator(tmp_path, fetched=_queued()))

        await orch.process_story(_queued())

        orch._provisioner.provision.assert_called_once()

    async def test_a_failed_re_read_launches_the_ticket_as_queued(self, tmp_path):
        orch = _launchable(_orchestrator(tmp_path, fetched=ProviderError("503")))

        await orch.process_story(_queued())

        orch._provisioner.provision.assert_called_once()

    @pytest.mark.parametrize(
        "error", [ProviderError("503"), aiohttp.ClientError("reset")]
    )
    async def test_a_failed_re_read_warns_and_records_in_flight(
        self, tmp_path, _isolated_state_dir, caplog, error
    ):
        orch = _launchable(_orchestrator(tmp_path, fetched=error))
        seen = {}

        async def invoke(**kwargs):
            entries = _read_state(_isolated_state_dir)["processed_stories"]
            seen["statuses"] = [e["status"] for e in entries]

        orch._claude_runner.invoke = AsyncMock(side_effect=invoke)

        with caplog.at_level(logging.WARNING, logger=_ORCH_LOGGER):
            await orch.process_story(_queued())

        assert "Could not re-check sc-7" in caplog.text
        assert "launching it as queued" in caplog.text
        assert seen["statuses"] == ["in_flight"]

    async def test_an_already_processed_ticket_is_not_re_read(
        self, tmp_path, _isolated_state_dir
    ):
        """The ledger guard runs first: a duplicate dequeue costs no API call."""
        orch = _launchable(_orchestrator(tmp_path, fetched=_queued()))
        record_processed_story(
            _isolated_state_dir,
            ProcessingRecord(
                story_id="sc-7",
                branch="sc-7",
                status="in_flight",
                processed_at=datetime.now(timezone.utc),
            ),
        )

        await orch.process_story(_queued())

        orch._scanners[0]._provider.fetch.assert_not_awaited()
        orch._provisioner.provision.assert_not_called()

    async def test_an_unassigned_ticket_is_skipped_without_a_re_read(
        self, tmp_path, _isolated_state_dir
    ):
        orch = _launchable(_orchestrator(tmp_path, fetched=_queued()))

        await orch.process_story(_queued(owner_ids=["someone-else"]))

        orch._scanners[0]._provider.fetch.assert_not_awaited()
        entries = _read_state(_isolated_state_dir)["processed_stories"]
        assert [e["status"] for e in entries] == ["skipped"]

    async def test_a_miss_writes_nothing_and_never_moves_the_ticket(
        self, tmp_path, _isolated_state_dir, caplog
    ):
        """Engine mode: ``SessionRunner.run`` is what moves the ticket into its
        start state, so a dropped ticket must never reach it."""
        orch = _orchestrator(tmp_path, fetched=_queued(state_id=UNSCHEDULED))
        orch._cs_runner = MagicMock()
        orch._cs_runner.run = AsyncMock()
        record_pending_story(_isolated_state_dir, "sc-7", 7, "sc")

        with caplog.at_level(logging.INFO, logger=_ORCH_LOGGER):
            await orch.process_story(_queued())

        orch._cs_runner.run.assert_not_awaited()
        orch._scanners[0]._provider.set_state.assert_not_called()
        assert _read_state(_isolated_state_dir).get("processed_stories", []) == []
        assert load_pending_stories(_isolated_state_dir) == []
        assert "Not launching sc-7" in caplog.text
        assert "since it was queued" in caplog.text

    async def test_a_webhook_event_is_not_re_checked(self, tmp_path):
        """The webhook path just fetched the ticket fresh; a second read would
        only cost an API call."""
        orch = _launchable(_orchestrator(tmp_path))
        orch._fetch_story = AsyncMock(return_value=_queued(state_id=UNSCHEDULED))
        orch._ingest_filter_miss_now = AsyncMock(return_value="should not be asked")
        event = WebhookEvent(
            event_id="evt-1",
            story_id=7,
            action_type="create",
            member_id="member-123",
            owner_ids=["member-123"],
            changed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            raw_payload={"actions": []},
        )

        await orch.process_story(event)

        orch._ingest_filter_miss_now.assert_not_awaited()
        orch._provisioner.provision.assert_called_once()

    async def test_a_passing_ticket_launches_the_queued_snapshot(self, tmp_path):
        """Only the filter decision is fresh; the launch still uses what was
        queued. Pinned so a switch to the fresh read is a deliberate change."""
        fresh = _queued(description="Edited while the ticket waited in the queue.")
        orch = _launchable(_orchestrator(tmp_path, fetched=fresh))
        queued = _queued()

        await orch.process_story(queued)

        (launched,) = orch._provisioner.provision.call_args.args
        assert launched is queued
        assert isinstance(launched, Ticket)


# --------------------------------------------------------------------------- #
# Shortcut feeds the check
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status, text=""):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        return self._resp


class TestShortcutFeedsTheCheck:
    def test_a_story_carries_its_workflow_state_id(self):
        t = story_from_api_response(
            {"id": 7, "name": "x", "workflow_state_id": int(WILL_DO)}
        )
        assert t.state_id == WILL_DO

    async def test_a_deleted_story_raises_ticket_not_found(self):
        prov = ShortcutProvider(_source())
        with patch("aiohttp.ClientSession", return_value=_Session(_Resp(404))):
            with pytest.raises(TicketNotFound):
                await prov.fetch("7")

    async def test_any_other_failure_stays_a_plain_provider_error(self):
        prov = ShortcutProvider(_source())
        with patch("aiohttp.ClientSession", return_value=_Session(_Resp(503))):
            with pytest.raises(ProviderError) as info:
                await prov.fetch("7")
        assert not isinstance(info.value, TicketNotFound)
