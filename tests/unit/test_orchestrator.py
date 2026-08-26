"""Unit tests for the PipelineOrchestrator."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ticket_ingestion.config import (
    EngineConfig,
    PipelineConfig,
    TicketProviderConfig,
)
from backend.ticket_ingestion.models import (
    ClarificationResult,
    ProcessingRecord,
    ProvisionedEnvironment,
    Ticket,
    ValidationResult,
    WebhookEvent,
)
from backend.ticket_ingestion import orchestrator as orchestrator_mod
from backend.ticket_ingestion.orchestrator import PipelineOrchestrator
from backend.ticket_ingestion.session_runner import SessionRunner, engine_bridge_error
from backend.ticket_ingestion.state import (
    load_processed_story_ids,
    record_processed_story,
)
from tests._factories import make_ticket


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point the orchestrator's ledger at a scratch dir.

    ``process_story`` now reads/writes the processed_stories ledger for real
    (idempotency pre-check + in-flight marker + completion update); without
    this the tests would hit ``./state.json`` in whatever cwd pytest runs in.
    """
    state_dir = tmp_path / "ledger"
    state_dir.mkdir()
    monkeypatch.setattr(orchestrator_mod, "_STATE_DIR", state_dir)
    return state_dir


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """Orchestrator unit tests must never spawn real processes.

    The fixture below builds a *real* ``PipelineOrchestrator`` (real
    ``ClaudeCodeRunner``). Any ``process_story`` path that reaches
    ``ClaudeCodeRunner.invoke`` without an explicit mock would run
    ``tmux new-session -s sc-<id>`` + ``claude`` for real — and the ingestion
    pipeline's testmon refresher runs this suite on every start, resurrecting an
    ``sc-<id>`` Claude session each reboot. Stub the async launcher so any such
    path is inert.
    """

    async def _fake_exec(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


@pytest.fixture
def config(tmp_path):
    """Create a test PipelineConfig."""
    return PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut",
            api_token="sc_test_token",
            member_id="member-uuid-123",
        ),
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "workspaces",
        min_description_length=20,
        log_file=tmp_path / "pipeline.log",
        log_level="INFO",
    )


@pytest.fixture
def orchestrator(config):
    """Create a PipelineOrchestrator with mocked components."""
    orch = PipelineOrchestrator(config)
    return orch


@pytest.fixture
def sample_story():
    """Create a sample Ticket for testing."""
    return make_ticket(
        id=12345,
        description="This is a test story with enough description length.",
        acceptance_criteria=["WHEN user clicks, THEN something happens"],
        owner_ids=["member-uuid-123"],
        created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def sample_webhook_event():
    """Create a sample WebhookEvent for testing."""
    return WebhookEvent(
        event_id="evt-001",
        story_id=12345,
        action_type="create",
        member_id="member-uuid-123",
        owner_ids=["member-uuid-123"],
        changed_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        raw_payload={"actions": []},
    )


class TestPipelineOrchestratorInit:
    """Tests for PipelineOrchestrator initialization."""

    def test_initializes_all_components(self, orchestrator):
        """All pipeline components are initialized."""
        assert orchestrator._scanners
        assert orchestrator._assignee_filter is not None
        assert orchestrator._validator is not None
        assert orchestrator._provisioner is not None
        assert orchestrator._claude_runner is not None
        assert orchestrator._clarification_handler is not None

    def test_queue_is_created(self, orchestrator):
        """A shared asyncio.Queue is created."""
        assert isinstance(orchestrator._queue, asyncio.Queue)

    def test_assignee_filter_uses_config_member_id(self, orchestrator, config):
        """AssigneeFilter is initialized with the configured member id(s)."""
        assert orchestrator._assignee_filter._member_ids == {config.ticketing.member_id}

    def test_an_any_assignee_source_disarms_the_filter(self, config):
        """The net is flock-wide and the queue doesn't record which source a
        story came from, so one source ingesting anyone's tickets has to switch
        it off — otherwise it would reject exactly the tickets that source
        exists to pick up."""
        config.ticketing_sources = [
            TicketProviderConfig(
                provider="shortcut",
                api_token="t",
                member_id="member-uuid-123",
                workflow_state="100",
                assignee_scope="anyone",
            )
        ]
        assert PipelineOrchestrator(config)._assignee_filter._member_ids == set()

    def test_an_unbounded_any_assignee_source_keeps_the_filter(self, config):
        """No ingest state means the scope never actually widened, so neither
        does the net."""
        config.ticketing_sources = [
            TicketProviderConfig(
                provider="shortcut",
                api_token="t",
                member_id="member-uuid-123",
                assignee_scope="anyone",
            )
        ]
        assert PipelineOrchestrator(config)._assignee_filter._member_ids == {
            "member-uuid-123"
        }


class TestProcessStory:
    """Tests for the process_story method."""

    @pytest.mark.asyncio
    async def test_processes_story_assigned_to_target(
        self, orchestrator, sample_story, _isolated_state_dir
    ):
        """A story assigned to the target member goes through the full pipeline."""
        env = ProvisionedEnvironment(
            directory=Path("/tmp/workspaces/shortcut-12345"),
            branch_name="shortcut/12345",
            cursor_window_id=999,
        )
        orchestrator._provisioner.provision = AsyncMock(return_value=env)
        orchestrator._claude_runner.invoke = AsyncMock()

        await orchestrator.process_story(sample_story)

        orchestrator._provisioner.provision.assert_called_once_with(sample_story)
        orchestrator._claude_runner.invoke.assert_called_once_with(
            env=env, story=sample_story, supplemental_context=None
        )
        # The in-flight marker was flipped to a single terminal record keyed
        # on the provider-scoped slug (no duplicate entries).
        from backend.ticket_ingestion.state import _read_state

        entries = _read_state(_isolated_state_dir).get("processed_stories", [])
        entries = [e for e in entries if e["story_id"] == "sc-12345"]
        assert len(entries) == 1
        assert entries[0]["status"] == "completed"
        assert entries[0]["branch"] == "shortcut/12345"

    @pytest.mark.asyncio
    async def test_skips_story_not_assigned_to_target(self, orchestrator):
        """A story not assigned to the target member is skipped."""
        story = make_ticket(
            id=99999,
            name="Other Story",
            description="Some description that is long enough for validation.",
            acceptance_criteria=["criterion"],
            owner_ids=["other-member-uuid"],
            created_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
        )
        orchestrator._provisioner.provision = AsyncMock()

        with patch(
            "backend.ticket_ingestion.orchestrator.record_processed_story"
        ) as mock_record:
            await orchestrator.process_story(story)

        orchestrator._provisioner.provision.assert_not_called()
        mock_record.assert_called_once()
        record = mock_record.call_args[0][1]
        assert record.status == "skipped"

    @pytest.mark.asyncio
    async def test_routes_through_clarification_on_validation_failure(
        self, orchestrator
    ):
        """A story that fails validation is routed through clarification."""
        story = make_ticket(
            id=11111,
            name="Bad Story",
            description="Short",
            acceptance_criteria=[],
            owner_ids=["member-uuid-123"],
            created_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
        )

        # The real validator is always-valid, so force an invalid result to
        # exercise the clarification branch of process_story.
        orchestrator._validator.validate = MagicMock(
            return_value=ValidationResult(is_valid=False, failures=["too short"])
        )

        clarification_result = ClarificationResult(
            action="provide_context",
            supplemental_context="Here is more context with enough detail for validation.",
        )
        orchestrator._clarification_handler.request_clarification = AsyncMock(
            return_value=clarification_result
        )

        env = ProvisionedEnvironment(
            directory=Path("/tmp/workspaces/shortcut-11111"),
            branch_name="shortcut/11111",
            cursor_window_id=888,
        )
        orchestrator._provisioner.provision = AsyncMock(return_value=env)
        orchestrator._claude_runner.invoke = AsyncMock()

        with patch("backend.ticket_ingestion.orchestrator.record_processed_story"):
            await orchestrator.process_story(story)

        orchestrator._clarification_handler.request_clarification.assert_called_once()
        orchestrator._provisioner.provision.assert_called_once()
        orchestrator._claude_runner.invoke.assert_called_once()
        # Supplemental context is passed to Claude runner
        call_kwargs = orchestrator._claude_runner.invoke.call_args[1]
        assert (
            call_kwargs["supplemental_context"]
            == clarification_result.supplemental_context
        )

    @pytest.mark.asyncio
    async def test_engine_mode_folds_clarification_into_single_session(
        self, orchestrator
    ):
        """In engine mode a validation-failing story does NOT spawn a separate
        standalone Cursor + terminal clarification session; the clarification ask is
        folded into the one MindFlock session as supplemental context."""
        story = make_ticket(
            id=22222,
            name="Low context",
            description="Short",
            acceptance_criteria=[],
            owner_ids=["member-uuid-123"],
            created_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
        )
        orchestrator._validator.validate = MagicMock(
            return_value=ValidationResult(is_valid=False, failures=["too short"])
        )
        # Engine enabled: route through the MindFlock SessionRunner.
        orchestrator._cs_runner = AsyncMock()
        orchestrator._cs_runner.run = AsyncMock(return_value="feature/shortcut-22222")
        orchestrator._clarification_handler.clarification_context = MagicMock(
            return_value="CLARIFY-ASK"
        )
        # The standalone clarification/provision/terminal path must NOT be touched.
        orchestrator._clarification_handler.request_clarification = AsyncMock()
        orchestrator._provisioner.provision = AsyncMock()
        orchestrator._claude_runner.invoke = AsyncMock()

        with patch("backend.ticket_ingestion.orchestrator.record_processed_story"):
            await orchestrator.process_story(story)

        # One engine session, seeded with the clarification ask; no standalone path.
        orchestrator._cs_runner.run.assert_called_once()
        assert (
            orchestrator._cs_runner.run.call_args[1]["supplemental_context"]
            == "CLARIFY-ASK"
        )
        orchestrator._clarification_handler.request_clarification.assert_not_called()
        orchestrator._provisioner.provision.assert_not_called()
        orchestrator._claude_runner.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_story_when_clarification_skipped(
        self, orchestrator, _isolated_state_dir
    ):
        """A story is skipped when the developer chooses to skip during clarification."""
        story = make_ticket(
            id=22222,
            name="Skip Story",
            description="Short",
            acceptance_criteria=[],
            owner_ids=["member-uuid-123"],
            created_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
        )

        # The real validator is always-valid, so force an invalid result to
        # exercise the clarification branch of process_story.
        orchestrator._validator.validate = MagicMock(
            return_value=ValidationResult(is_valid=False, failures=["too short"])
        )

        clarification_result = ClarificationResult(action="skip")
        orchestrator._clarification_handler.request_clarification = AsyncMock(
            return_value=clarification_result
        )
        orchestrator._provisioner.provision = AsyncMock()
        orchestrator._claude_runner.invoke = AsyncMock()

        await orchestrator.process_story(story)

        orchestrator._provisioner.provision.assert_not_called()
        # The in-flight marker was flipped in place to a single skipped record.
        from backend.ticket_ingestion.state import _read_state

        entries = _read_state(_isolated_state_dir).get("processed_stories", [])
        entries = [e for e in entries if e["story_id"] == "sc-22222"]
        assert len(entries) == 1
        assert entries[0]["status"] == "skipped"
        assert "clarification" in entries[0]["failure_reason"]

    @pytest.mark.asyncio
    async def test_fetches_full_story_for_webhook_event(
        self, orchestrator, sample_webhook_event, sample_story
    ):
        """WebhookEvent items trigger a fetch of the full story from the API."""
        orchestrator._fetch_story = AsyncMock(return_value=sample_story)

        env = ProvisionedEnvironment(
            directory=Path("/tmp/workspaces/shortcut-12345"),
            branch_name="shortcut/12345",
            cursor_window_id=999,
        )
        orchestrator._provisioner.provision = AsyncMock(return_value=env)
        orchestrator._claude_runner.invoke = AsyncMock()

        with patch("backend.ticket_ingestion.orchestrator.record_processed_story"):
            await orchestrator.process_story(sample_webhook_event)

        orchestrator._fetch_story.assert_called_once_with(12345)
        orchestrator._provisioner.provision.assert_called_once_with(sample_story)


class TestIdempotency:
    """The in-flight TOCTOU fix: a story is recorded the moment work starts,
    and duplicates are dropped at dequeue time."""

    @pytest.mark.asyncio
    async def test_in_flight_marker_written_before_session_runs(
        self, orchestrator, sample_story, _isolated_state_dir
    ):
        """The ledger blocks re-ingestion WHILE the (long) session runs, not
        only after it completes — the original TOCTOU window."""
        from backend.ticket_ingestion.state import _read_state

        env = ProvisionedEnvironment(
            directory=Path("/tmp/workspaces/shortcut-12345"),
            branch_name="shortcut/12345",
            cursor_window_id=999,
        )
        orchestrator._provisioner.provision = AsyncMock(return_value=env)

        seen_mid_session = {}

        async def invoke_side_effect(**kwargs):
            # Snapshot the ledger as a concurrent scan would see it.
            entries = _read_state(_isolated_state_dir).get("processed_stories", [])
            seen_mid_session["ids"] = load_processed_story_ids(_isolated_state_dir)
            seen_mid_session["statuses"] = [
                e["status"] for e in entries if e["story_id"] == "sc-12345"
            ]

        orchestrator._claude_runner.invoke = AsyncMock(side_effect=invoke_side_effect)

        await orchestrator.process_story(sample_story)

        # Mid-session the story was already recorded as in_flight and
        # load_processed_story_ids (the backfill guard) filtered it.
        assert seen_mid_session["statuses"] == ["in_flight"]
        assert "sc-12345" in seen_mid_session["ids"]
        # Afterwards the same entry is terminal — no duplicate rows.
        entries = _read_state(_isolated_state_dir).get("processed_stories", [])
        entries = [e for e in entries if e["story_id"] == "sc-12345"]
        assert len(entries) == 1
        assert entries[0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_duplicate_dequeue_is_dropped(
        self, orchestrator, sample_story, _isolated_state_dir
    ):
        """A duplicate that slipped into the queue while the story was
        in-flight (or already processed) is dropped without provisioning."""
        from datetime import datetime, timezone

        record_processed_story(
            _isolated_state_dir,
            ProcessingRecord(
                story_id="sc-12345",
                branch="sc-12345",
                status="in_flight",
                processed_at=datetime.now(timezone.utc),
            ),
        )
        orchestrator._provisioner.provision = AsyncMock()
        orchestrator._claude_runner.invoke = AsyncMock()

        await orchestrator.process_story(sample_story)

        orchestrator._provisioner.provision.assert_not_called()
        orchestrator._claude_runner.invoke.assert_not_called()
        # The existing in-flight record is untouched (no second entry).
        from backend.ticket_ingestion.state import _read_state

        entries = _read_state(_isolated_state_dir).get("processed_stories", [])
        assert [e["status"] for e in entries] == ["in_flight"]

    @pytest.mark.asyncio
    async def test_failure_flips_marker_to_failed(
        self, orchestrator, sample_story, _isolated_state_dir
    ):
        """A session failure leaves a terminal 'failed' record (manual unblock:
        delete the entry) instead of a dangling in_flight or silent retry."""
        orchestrator._provisioner.provision = AsyncMock(
            side_effect=RuntimeError("provisioning exploded")
        )
        orchestrator._claude_runner.invoke = AsyncMock()

        with pytest.raises(RuntimeError):
            await orchestrator.process_story(sample_story)

        from backend.ticket_ingestion.state import _read_state

        entries = _read_state(_isolated_state_dir).get("processed_stories", [])
        entries = [e for e in entries if e["story_id"] == "sc-12345"]
        assert len(entries) == 1
        assert entries[0]["status"] == "failed"
        assert "provisioning exploded" in entries[0]["failure_reason"]
        # And it stays blocked from re-ingestion.
        assert "sc-12345" in load_processed_story_ids(_isolated_state_dir)


class TestProcessPR:
    """_process_pr: fetch-failure retry semantics and the provisioning cap."""

    @pytest.fixture
    def pr_orchestrator(self, orchestrator):
        from backend.ticket_ingestion.config import GithubConfig
        from backend.ticket_ingestion.models import ProvisionedPRWorkspace

        orchestrator.config.github = GithubConfig(
            repos=["org/repo"],
            base_branch="main",
            min_age_minutes=15,
            poll_interval_seconds=60,
            enabled=True,
            skip_authors=[],
            token="tok",
        )
        orchestrator._pr_provisioner.provision = AsyncMock(
            return_value=ProvisionedPRWorkspace(
                directory=Path("/tmp/pr-ws"), head_ref="feat", head_sha="abc"
            )
        )
        orchestrator._pr_runner.launch = AsyncMock()
        return orchestrator

    def _pr(self):
        from backend.ticket_ingestion.models import PullRequest

        return PullRequest(
            number=9,
            head_ref="feat",
            head_sha="abc",
            base_ref="main",
            title="t",
            url="u",
            author="me",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            repo="org/repo",
        )

    @pytest.mark.asyncio
    async def test_fetch_failure_does_not_record_pr(
        self, pr_orchestrator, _isolated_state_dir
    ):
        """A transient GitHub error must NOT read as 'no comments' and record
        the PR processed — the next poll has to retry the fetch."""
        from backend.ticket_ingestion.pr_comments import PRCommentsFetchError
        from backend.ticket_ingestion.state import load_processed_prs

        with patch(
            "backend.ticket_ingestion.orchestrator.fetch_actionable_comments",
            new_callable=AsyncMock,
            side_effect=PRCommentsFetchError("502 from GraphQL"),
        ):
            await pr_orchestrator._process_pr(self._pr())

        assert load_processed_prs(_isolated_state_dir) == set()
        pr_orchestrator._pr_provisioner.provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_comments_still_recorded(
        self, pr_orchestrator, _isolated_state_dir
    ):
        from backend.ticket_ingestion.state import load_processed_prs

        with patch(
            "backend.ticket_ingestion.orchestrator.fetch_actionable_comments",
            new_callable=AsyncMock,
            return_value=[],
        ):
            await pr_orchestrator._process_pr(self._pr())

        assert ("org/repo", 9) in load_processed_prs(_isolated_state_dir)

    @pytest.mark.asyncio
    async def test_provisioning_failures_capped(
        self, pr_orchestrator, _isolated_state_dir
    ):
        """A PR whose provisioning keeps failing is retried a bounded number of
        times, then recorded processed-as-failed instead of being re-cloned on
        every poll forever."""
        import json

        from backend.ticket_ingestion.models import PRComment
        from backend.ticket_ingestion.orchestrator import _PR_MAX_ATTEMPTS
        from backend.ticket_ingestion.state import load_processed_prs

        comment = PRComment(id=1, kind="review", author="rev", body="fix", url="u")
        pr_orchestrator._pr_provisioner.provision = AsyncMock(
            side_effect=RuntimeError("clone exploded")
        )
        with patch(
            "backend.ticket_ingestion.orchestrator.fetch_actionable_comments",
            new_callable=AsyncMock,
            return_value=[comment],
        ):
            for _ in range(_PR_MAX_ATTEMPTS - 1):
                with pytest.raises(RuntimeError):
                    await pr_orchestrator._process_pr(self._pr())
                assert load_processed_prs(_isolated_state_dir) == set()
            # Final attempt: swallowed, recorded processed-as-failed.
            await pr_orchestrator._process_pr(self._pr())

        assert ("org/repo", 9) in load_processed_prs(_isolated_state_dir)
        data = json.loads((_isolated_state_dir / "state.json").read_text())
        assert data["processed_prs"][0]["status"] == "failed"
        # The attempt counter was cleared for a future manual unblock.
        assert data.get("pr_attempts", {}) == {}

    @pytest.mark.asyncio
    async def test_success_records_and_clears_attempts(
        self, pr_orchestrator, _isolated_state_dir
    ):
        import json

        from backend.ticket_ingestion.models import PRComment
        from backend.ticket_ingestion.state import (
            load_processed_prs,
            record_pr_attempt,
        )

        record_pr_attempt(_isolated_state_dir, "org/repo", 9)  # a prior failure
        comment = PRComment(id=1, kind="review", author="rev", body="fix", url="u")
        with patch(
            "backend.ticket_ingestion.orchestrator.fetch_actionable_comments",
            new_callable=AsyncMock,
            return_value=[comment],
        ):
            await pr_orchestrator._process_pr(self._pr())

        assert ("org/repo", 9) in load_processed_prs(_isolated_state_dir)
        data = json.loads((_isolated_state_dir / "state.json").read_text())
        assert "status" not in data["processed_prs"][0]
        assert data.get("pr_attempts", {}) == {}


class TestProcessIssue:
    """_process_issue: fetch-failure retry semantics and the provisioning cap.

    Mirrors TestProcessPR for the parallel issue-handling path (same
    give-up-after-cap contract with IssueCommentsFetchError and
    _ISSUE_MAX_ATTEMPTS), so the two paths can't silently drift.
    """

    @pytest.fixture
    def issue_orchestrator(self, orchestrator):
        from backend.ticket_ingestion.config import GithubConfig

        orchestrator.config.github = GithubConfig(
            repos=["org/repo"],
            base_branch="main",
            min_age_minutes=15,
            poll_interval_seconds=60,
            enabled=True,
            skip_authors=[],
            token="tok",
        )
        # The orchestrator built by the `orchestrator` fixture had github=None,
        # so _issue_monitor is None; wire a stub exposing the fetch_comments
        # seam these tests drive. The engine (_cs_runner) is off, so the
        # provision + invoke path is taken.
        orchestrator._issue_monitor = MagicMock()
        orchestrator._issue_monitor.fetch_comments = AsyncMock(return_value=[])
        orchestrator._provisioner.provision = AsyncMock(
            return_value=ProvisionedEnvironment(
                directory=Path("/tmp/issue-ws"),
                branch_name="feature/issue-repo-9/t",
                cursor_window_id=0,
            )
        )
        orchestrator._claude_runner.invoke = AsyncMock()
        return orchestrator

    def _issue(self):
        from backend.ticket_ingestion.models import Issue

        return Issue(
            number=9,
            title="t",
            body="do the thing with enough description length here",
            url="u",
            author="me",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            repo="org/repo",
        )

    @pytest.mark.asyncio
    async def test_fetch_failure_does_not_record_issue(
        self, issue_orchestrator, _isolated_state_dir
    ):
        """A transient GitHub comment-fetch failure must leave the issue
        unrecorded so the next poll retries."""
        from backend.ticket_ingestion.issue_monitor import IssueCommentsFetchError
        from backend.ticket_ingestion.state import load_processed_issues

        issue_orchestrator._issue_monitor.fetch_comments = AsyncMock(
            side_effect=IssueCommentsFetchError("502 from GitHub")
        )
        await issue_orchestrator._process_issue(self._issue())

        assert load_processed_issues(_isolated_state_dir) == set()
        issue_orchestrator._provisioner.provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_records_and_clears_attempts(
        self, issue_orchestrator, _isolated_state_dir
    ):
        import json

        from backend.ticket_ingestion.state import (
            load_processed_issues,
            record_issue_attempt,
        )

        record_issue_attempt(_isolated_state_dir, "org/repo", 9)  # a prior failure
        await issue_orchestrator._process_issue(self._issue())

        assert ("org/repo", 9) in load_processed_issues(_isolated_state_dir)
        data = json.loads((_isolated_state_dir / "state.json").read_text())
        assert "status" not in data["processed_issues"][0]  # recorded normally
        assert data.get("issue_attempts", {}) == {}  # counter cleared
        issue_orchestrator._provisioner.provision.assert_called_once()
        issue_orchestrator._claude_runner.invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_provisioning_failures_capped(
        self, issue_orchestrator, _isolated_state_dir
    ):
        """An issue whose provisioning keeps failing is retried a bounded number
        of times, then recorded processed-as-failed instead of re-cloned on
        every poll forever."""
        import json

        from backend.ticket_ingestion.orchestrator import _ISSUE_MAX_ATTEMPTS
        from backend.ticket_ingestion.state import load_processed_issues

        issue_orchestrator._provisioner.provision = AsyncMock(
            side_effect=RuntimeError("clone exploded")
        )
        for _ in range(_ISSUE_MAX_ATTEMPTS - 1):
            with pytest.raises(RuntimeError):
                await issue_orchestrator._process_issue(self._issue())
            assert load_processed_issues(_isolated_state_dir) == set()
        # Final attempt: swallowed, recorded processed-as-failed.
        await issue_orchestrator._process_issue(self._issue())

        assert ("org/repo", 9) in load_processed_issues(_isolated_state_dir)
        data = json.loads((_isolated_state_dir / "state.json").read_text())
        assert data["processed_issues"][0]["status"] == "failed"
        # The attempt counter was cleared for a future manual unblock.
        assert data.get("issue_attempts", {}) == {}


class TestRun:
    """Tests for the run() method lifecycle."""

    @pytest.mark.asyncio
    async def test_backfill_failure_does_not_prevent_main_loop(self, orchestrator):
        """If the backfill scan fails, run() still enters the main processing loop.

        The initial backfill scan raises, but run() catches it and proceeds to
        drain the queue. We enqueue one assigned story and assert it is still
        processed despite the backfill failure.
        """
        scan_calls = 0

        async def scan_side_effect():
            nonlocal scan_calls
            scan_calls += 1
            # First (startup) scan fails; poll-loop scans return nothing.
            if scan_calls == 1:
                raise RuntimeError("API unreachable")
            return 0

        orchestrator._scanners[0].scan = AsyncMock(side_effect=scan_side_effect)

        story = make_ticket(
            id=333,
            name="Assigned Story",
            description="A story assigned to the target member after backfill fails.",
            acceptance_criteria=["criterion"],
            owner_ids=["member-uuid-123"],
            created_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
        )
        await orchestrator._queue.put(story)

        env = ProvisionedEnvironment(
            directory=Path("/tmp/workspaces/shortcut-333"),
            branch_name="shortcut/333",
            cursor_window_id=333,
        )
        orchestrator._provisioner.provision = AsyncMock(return_value=env)
        orchestrator._claude_runner.invoke = AsyncMock()

        async def cancel_when_done():
            while not orchestrator._queue.empty():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            raise asyncio.CancelledError()

        with patch("backend.ticket_ingestion.orchestrator.record_processed_story"):
            with pytest.raises(asyncio.CancelledError):
                task = asyncio.create_task(orchestrator.run())
                cancel_task = asyncio.create_task(cancel_when_done())
                await asyncio.gather(task, cancel_task)

        # Startup scan was attempted and the story was still provisioned.
        assert scan_calls >= 1
        orchestrator._provisioner.provision.assert_called_once_with(story)
        orchestrator._claude_runner.invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_per_story_error_does_not_crash_pipeline(self, orchestrator):
        """An exception in process_story is caught and the pipeline continues."""
        story1 = make_ticket(
            id=111,
            name="Failing Story",
            description="This story will cause an error during processing.",
            acceptance_criteria=["criterion"],
            owner_ids=["member-uuid-123"],
            created_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
        )
        story2 = make_ticket(
            id=222,
            name="Good Story",
            description="This story should process fine after the first fails.",
            acceptance_criteria=["criterion"],
            owner_ids=["member-uuid-123"],
            created_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
        )

        # Pre-fill the queue
        await orchestrator._queue.put(story1)
        await orchestrator._queue.put(story2)

        orchestrator._scanners[0].scan = AsyncMock(return_value=0)

        # Make provisioner fail for story1, succeed for story2
        env = ProvisionedEnvironment(
            directory=Path("/tmp/workspaces/shortcut-222"),
            branch_name="shortcut/222",
            cursor_window_id=777,
        )
        call_count = 0

        async def provision_side_effect(story):
            nonlocal call_count
            call_count += 1
            if story.id == 111:
                raise RuntimeError("Provisioning failed!")
            return env

        orchestrator._provisioner.provision = AsyncMock(
            side_effect=provision_side_effect
        )
        orchestrator._claude_runner.invoke = AsyncMock()

        processed = []
        original_process = orchestrator.process_story

        async def track_process(item):
            await original_process(item)
            processed.append(item.id)

        orchestrator.process_story = track_process

        # Cancel after both items are processed
        async def cancel_when_done():
            while not orchestrator._queue.empty():
                await asyncio.sleep(0.01)
            # Give a moment for the last task_done
            await asyncio.sleep(0.05)
            raise asyncio.CancelledError()

        with patch("backend.ticket_ingestion.orchestrator.record_processed_story"):
            with pytest.raises(asyncio.CancelledError):
                task = asyncio.create_task(orchestrator.run())
                cancel_task = asyncio.create_task(cancel_when_done())
                await asyncio.gather(task, cancel_task)

        # Story 2 was still processed despite story 1 failing
        assert 222 in processed


class TestFetchStory:
    """Tests for the _fetch_story helper method."""

    @pytest.mark.asyncio
    async def test_fetch_story_calls_shortcut_api(self, orchestrator):
        """_fetch_story makes a GET request to the Shortcut API."""
        mock_response_data = {
            "id": 12345,
            "name": "Fetched Story",
            "description": "A story fetched from the API.",
            "owner_ids": ["member-uuid-123"],
            "app_url": "https://app.shortcut.com/story/12345",
            "created_at": "2025-01-15T10:00:00Z",
        }

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.json = AsyncMock(return_value=mock_response_data)

            mock_session.get = MagicMock(return_value=AsyncMock())
            mock_session.get.return_value.__aenter__ = AsyncMock(
                return_value=mock_response
            )
            mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

            story = await orchestrator._fetch_story(12345)

        assert story.id == 12345
        assert story.name == "Fetched Story"

    @pytest.mark.asyncio
    async def test_fetch_story_raises_on_api_error(self, orchestrator):
        """_fetch_story raises RuntimeError on non-200 response."""
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_response = AsyncMock()
            mock_response.status = 404
            mock_response.text = AsyncMock(return_value="Not Found")

            mock_session.get = MagicMock(return_value=AsyncMock())
            mock_session.get.return_value.__aenter__ = AsyncMock(
                return_value=mock_response
            )
            mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(RuntimeError, match="404"):
                await orchestrator._fetch_story(99999)


class TestEngineRunnerSelection:
    """Which launcher the orchestrator picks, and the one honest fallback.

    Engine mode is the shipped default because the bridge is in-process — no
    server to reach — so the only reason to drop to the standalone tmux +
    terminal-tab launcher is an environment where the engine half of the package
    does not import. That downgrade must be visible in the log, not silent.
    """

    def _engine_config(self, config: PipelineConfig) -> PipelineConfig:
        config.engine = EngineConfig()  # defaults: enabled, worktree
        return config

    def test_engine_config_defaults_select_the_session_runner(self, config) -> None:
        orch = PipelineOrchestrator(self._engine_config(config))
        assert isinstance(orch._cs_runner, SessionRunner)

    def test_engine_disabled_selects_the_standalone_launcher(self, config) -> None:
        config.engine = EngineConfig(enabled=False)
        orch = PipelineOrchestrator(config)
        assert orch._cs_runner is None

    def test_unimportable_engine_falls_back_and_warns(
        self, config, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(
            orchestrator_mod,
            "engine_bridge_error",
            lambda: "ModuleNotFoundError: No module named 'backend.session'",
        )
        with caplog.at_level("WARNING", logger=orchestrator_mod.__name__):
            orch = PipelineOrchestrator(self._engine_config(config))

        assert orch._cs_runner is None
        assert "falling back to the standalone launcher" in caplog.text
        # The reason is named so the log says WHY the product downgraded.
        assert "No module named 'backend.session'" in caplog.text

    def test_bridge_probe_passes_in_this_environment(self) -> None:
        """The real probe must succeed where the engine is installed — otherwise
        every install would silently ship the terminal-tab path."""
        assert engine_bridge_error() is None
