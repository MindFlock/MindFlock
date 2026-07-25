"""Unit tests for the InteractiveClarificationHandler."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.ticket_ingestion.clarification import InteractiveClarificationHandler
from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion.models import (
    ClarificationResult,
    ProvisionedEnvironment,
    Ticket,
    ValidationResult,
)
from tests._factories import make_ticket


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
        workspace_dir=Path("/tmp/workspaces"),
        min_description_length=20,
        log_file=Path("/tmp/pipeline.log"),
        log_level="INFO",
    )


@pytest.fixture
def sample_story():
    """Create a sample Ticket for testing."""
    return make_ticket(
        id=12345,
        name="Fix login bug",
        description="Short",
        acceptance_criteria=[],
        created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def validation_result():
    """Create a sample failed ValidationResult."""
    return ValidationResult(
        is_valid=False,
        failures=[
            "Description is too short (5 characters). A minimum of 20 characters is required.",
            "No acceptance criteria found. At least one acceptance criterion "
            "or expected behavior statement is required.",
        ],
    )


@pytest.fixture
def handler(config):
    """Create an InteractiveClarificationHandler instance."""
    return InteractiveClarificationHandler(config)


class TestInteractiveClarificationHandler:
    """Tests for InteractiveClarificationHandler."""

    def test_init_creates_provisioner_and_runner(self, handler, config):
        """Test that __init__ creates EnvironmentProvisioner and ClaudeCodeRunner."""
        assert handler.config is config
        assert handler._provisioner is not None
        assert handler._claude_runner is not None

    def test_build_clarification_prompt_includes_story_title(
        self, handler, sample_story, validation_result
    ):
        """Test that the clarification prompt includes the story title."""
        prompt = handler._build_clarification_prompt(sample_story, validation_result)
        assert "Fix login bug" in prompt
        assert "SC-12345" in prompt

    def test_build_clarification_prompt_includes_description(
        self, handler, sample_story, validation_result
    ):
        """Test that the clarification prompt includes the story description."""
        prompt = handler._build_clarification_prompt(sample_story, validation_result)
        assert "Short" in prompt

    def test_build_clarification_prompt_includes_validation_failures(
        self, handler, sample_story, validation_result
    ):
        """Test that the clarification prompt includes all validation failures."""
        prompt = handler._build_clarification_prompt(sample_story, validation_result)
        assert "Description is too short" in prompt
        assert "No acceptance criteria found" in prompt

    def test_build_clarification_prompt_includes_instructions(
        self, handler, sample_story, validation_result
    ):
        """Test that the prompt includes instructions to ask for context."""
        prompt = handler._build_clarification_prompt(sample_story, validation_result)
        assert "ask the developer for additional context" in prompt
        assert "skip this story" in prompt

    def test_build_clarification_prompt_empty_description(
        self, handler, validation_result
    ):
        """Test prompt when story has empty description."""
        story = make_ticket(
            id=99999,
            name="Empty story",
            description="   ",
            acceptance_criteria=[],
            created_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        )
        prompt = handler._build_clarification_prompt(story, validation_result)
        assert "(No description provided)" in prompt

    @pytest.mark.asyncio
    async def test_request_clarification_returns_provide_context(
        self, handler, sample_story, validation_result
    ):
        """Test that request_clarification returns action='provide_context'."""
        mock_env = ProvisionedEnvironment(
            directory=Path("/tmp/workspaces/shortcut-12345"),
            branch_name="shortcut/12345",
            cursor_window_id=123456,
        )

        with (
            patch.object(
                handler._provisioner, "provision", new_callable=AsyncMock
            ) as mock_provision,
            patch.object(
                handler, "_invoke_clarification_session", new_callable=AsyncMock
            ) as mock_invoke,
        ):
            mock_provision.return_value = mock_env

            result = await handler.request_clarification(
                sample_story, validation_result
            )

            assert isinstance(result, ClarificationResult)
            assert result.action == "provide_context"
            mock_provision.assert_called_once_with(sample_story)
            mock_invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_request_clarification_provisions_environment(
        self, handler, sample_story, validation_result
    ):
        """Test that request_clarification provisions the environment."""
        mock_env = ProvisionedEnvironment(
            directory=Path("/tmp/workspaces/shortcut-12345"),
            branch_name="shortcut/12345",
            cursor_window_id=123456,
        )

        with (
            patch.object(
                handler._provisioner, "provision", new_callable=AsyncMock
            ) as mock_provision,
            patch.object(
                handler, "_invoke_clarification_session", new_callable=AsyncMock
            ),
        ):
            mock_provision.return_value = mock_env

            await handler.request_clarification(sample_story, validation_result)

            mock_provision.assert_called_once_with(sample_story)

    @pytest.mark.asyncio
    async def test_request_clarification_invokes_claude_with_prompt(
        self, handler, sample_story, validation_result
    ):
        """Test that request_clarification invokes Claude Code with clarification prompt."""
        mock_env = ProvisionedEnvironment(
            directory=Path("/tmp/workspaces/shortcut-12345"),
            branch_name="shortcut/12345",
            cursor_window_id=123456,
        )

        with (
            patch.object(
                handler._provisioner, "provision", new_callable=AsyncMock
            ) as mock_provision,
            patch.object(
                handler, "_invoke_clarification_session", new_callable=AsyncMock
            ) as mock_invoke,
        ):
            mock_provision.return_value = mock_env

            await handler.request_clarification(sample_story, validation_result)

            # Verify the invoke was called with the env, a prompt string, and the story
            call_args = mock_invoke.call_args
            assert call_args[0][0] is mock_env  # env
            assert "Clarification Needed" in call_args[0][1]  # prompt
            assert call_args[0][2] is sample_story  # story

    @pytest.mark.asyncio
    async def test_request_clarification_propagates_provisioning_error(
        self, handler, sample_story, validation_result
    ):
        """Test that provisioning errors propagate up."""
        from backend.ticket_ingestion.provisioner import ProvisioningError

        with patch.object(
            handler._provisioner, "provision", new_callable=AsyncMock
        ) as mock_provision:
            mock_provision.side_effect = ProvisioningError("Clone failed")

            with pytest.raises(ProvisioningError, match="Clone failed"):
                await handler.request_clarification(sample_story, validation_result)


class TestClarificationContext:
    """Engine-mode context: the prompt only, no provisioning / session."""

    def test_context_equals_built_prompt(
        self, handler, sample_story, validation_result
    ):
        assert handler.clarification_context(
            sample_story, validation_result
        ) == handler._build_clarification_prompt(sample_story, validation_result)


class TestInvokeClarificationSession:
    """The standalone-session path: writes a prompt file, then invokes Claude."""

    @pytest.mark.asyncio
    async def test_writes_prompt_file_and_invokes(
        self, handler, sample_story, tmp_path, monkeypatch
    ):
        env = ProvisionedEnvironment(
            directory=tmp_path, branch_name="b", cursor_window_id=0
        )
        # Confine the temp prompt file to tmp_path so it doesn't leak into /tmp.
        real_mkstemp = __import__("tempfile").mkstemp
        monkeypatch.setattr(
            "backend.ticket_ingestion.clarification.tempfile.mkstemp",
            lambda *a, **k: real_mkstemp(
                prefix=k.get("prefix", ""),
                suffix=k.get("suffix", ""),
                dir=str(tmp_path),
            ),
        )
        with patch.object(
            handler._claude_runner, "invoke", new_callable=AsyncMock
        ) as mock_invoke:
            await handler._invoke_clarification_session(env, "the prompt", sample_story)
        _, kwargs = mock_invoke.call_args
        assert kwargs["supplemental_context"] == "the prompt"
        assert kwargs["env"] is env

    @pytest.mark.asyncio
    async def test_write_failure_cleans_up_and_reraises(
        self, handler, sample_story, tmp_path, monkeypatch
    ):
        env = ProvisionedEnvironment(
            directory=tmp_path, branch_name="b", cursor_window_id=0
        )
        created = {}
        real_mkstemp = __import__("tempfile").mkstemp

        def tracking_mkstemp(*a, **k):
            fd, path = real_mkstemp(
                prefix=k.get("prefix", ""),
                suffix=k.get("suffix", ""),
                dir=str(tmp_path),
            )
            created["path"] = path
            return fd, path

        monkeypatch.setattr(
            "backend.ticket_ingestion.clarification.tempfile.mkstemp",
            tracking_mkstemp,
        )
        # The prompt-file write blows up -> the handler must unlink the temp
        # file it created and re-raise.
        monkeypatch.setattr(
            "builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        )
        with pytest.raises(OSError, match="disk full"):
            await handler._invoke_clarification_session(env, "prompt", sample_story)
        assert not Path(created["path"]).exists()
