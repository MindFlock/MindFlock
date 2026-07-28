"""Integration tests for the ClaudeCodeRunner.

ClaudeCodeRunner.invoke() launches Claude Code inside a detached tmux session
per story and then opens a Windows Terminal tab that attaches to it. These
tests mock asyncio.create_subprocess_exec so NO real tmux/claude/wt.exe is ever
spawned, and verify the tmux-based command sequence:

    tmux kill-session (cleanup) -> tmux new-session (launch claude)
      -> tmux set-option (mouse / history-limit) -> wt.exe (attach tab)

Also verifies the prompt contains all required sections.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.ticket_ingestion.claude_runner import (
    ClaudeCodeRunner,
    _tmux_session_name,
)
from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion.models import ProvisionedEnvironment, Ticket
from tests._factories import make_ticket


def _make_config() -> PipelineConfig:
    """Helper to create a minimal PipelineConfig for testing."""
    return PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut",
            api_token="test-token",
            member_id="member-123",
        ),
        repo_url="git@github.com:org/example-bot.git",
        workspace_dir=Path("/tmp/workspaces"),
        min_description_length=20,
        log_file=Path("/tmp/pipeline.log"),
        log_level="INFO",
    )


def _make_story(story_id: int = 12345) -> Ticket:
    """Helper to create a test Ticket."""
    return make_ticket(
        id=story_id,
        name="Implement user authentication",
        description="Add JWT-based authentication to the API endpoints with refresh token support.",
        acceptance_criteria=[
            "WHEN a user logs in with valid credentials THEN a JWT token is returned",
            "WHEN a token expires THEN the refresh token can be used to get a new one",
            "WHEN invalid credentials are provided THEN a 401 response is returned",
        ],
        created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_environment(directory: Path | None = None) -> ProvisionedEnvironment:
    """Helper to create a test ProvisionedEnvironment."""
    return ProvisionedEnvironment(
        directory=directory or Path("/tmp/workspaces/shortcut-12345"),
        branch_name="shortcut/12345",
        cursor_window_id=98765432,
    )


def _make_successful_process() -> AsyncMock:
    """Create a mock process that completes successfully.

    Supports both the ``.communicate()`` path (new-session / set-option /
    wt.exe) and the ``.wait()`` path (kill-session).
    """
    process = AsyncMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(b"", b""))
    process.wait = AsyncMock(return_value=0)
    return process


def _make_failed_process(stderr: bytes = b"error: something failed") -> AsyncMock:
    """Create a mock process that fails."""
    process = AsyncMock()
    process.returncode = 1
    process.communicate = AsyncMock(return_value=(b"", stderr))
    process.wait = AsyncMock(return_value=1)
    return process


def _cmd(call) -> tuple:
    """Extract the positional argv tuple from a mock call."""
    return call[0]


class TestInvokeCommandSequence:
    """Tests that invoke() drives tmux + wt.exe in the correct sequence."""

    @pytest.fixture(autouse=True)
    def _force_wsl_terminal(self):
        """Pin the host to WSL with a resolvable Windows Terminal so the
        command-sequence assertions don't depend on where the suite runs.

        ``build_terminal_tab_argv`` now degrades to a no-op when the terminal
        emulator can't be resolved on PATH; without this the wt.exe step would
        vanish on any host lacking it (native Linux CI, WSL without interop).
        """
        with (
            patch("backend.osenv.os_kind", return_value="wsl"),
            patch(
                "backend.ticket_ingestion.terminal_launch.shutil.which",
                side_effect=lambda cmd: cmd,
            ),
            patch(
                "backend.ticket_ingestion.terminal_launch." "wsl_interop_available",
                return_value=True,
            ),
        ):
            yield

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_kill_existing_session_first(self, mock_exec: AsyncMock) -> None:
        """The first subprocess call kills any stale tmux session by name."""
        mock_exec.return_value = _make_successful_process()

        runner = ClaudeCodeRunner(_make_config())
        story = _make_story()
        await runner.invoke(_make_environment(), story)

        args = _cmd(mock_exec.call_args_list[0])
        assert args[0] == "tmux"
        assert args[1] == "kill-session"
        assert "-t" in args
        assert _tmux_session_name(story) in args

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_new_session_launches_claude(self, mock_exec: AsyncMock) -> None:
        """The second call is a detached tmux new-session that launches claude."""
        mock_exec.return_value = _make_successful_process()

        runner = ClaudeCodeRunner(_make_config())
        story = _make_story()
        await runner.invoke(_make_environment(), story)

        args = _cmd(mock_exec.call_args_list[1])
        assert args[0] == "tmux"
        assert args[1] == "new-session"
        assert "-d" in args  # detached
        # session name follows -s
        assert args[args.index("-s") + 1] == _tmux_session_name(story)
        # the shell command (last arg) invokes claude
        inner = args[-1]
        assert "claude" in inner

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_new_session_runs_in_workdir(self, mock_exec: AsyncMock) -> None:
        """new-session sets the working directory via -c to the env directory."""
        mock_exec.return_value = _make_successful_process()

        runner = ClaudeCodeRunner(_make_config())
        workdir = Path("/tmp/workspaces/shortcut-999")
        env = _make_environment(directory=workdir)
        await runner.invoke(env, _make_story())

        args = _cmd(mock_exec.call_args_list[1])
        assert args[args.index("-c") + 1] == str(workdir)

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_inner_command_references_prompt_file(
        self, mock_exec: AsyncMock
    ) -> None:
        """The launched shell command reads the prompt from a temp prompt file."""
        mock_exec.return_value = _make_successful_process()

        runner = ClaudeCodeRunner(_make_config())
        await runner.invoke(_make_environment(), _make_story())

        inner = _cmd(mock_exec.call_args_list[1])[-1]
        assert "claude_prompt_" in inner
        assert ".md" in inner
        # prompt is streamed into claude via cat
        assert "cat " in inner

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_set_option_calls_configure_session(
        self, mock_exec: AsyncMock
    ) -> None:
        """After launch, tmux set-option enables mouse and bumps history-limit."""
        mock_exec.return_value = _make_successful_process()

        runner = ClaudeCodeRunner(_make_config())
        story = _make_story()
        await runner.invoke(_make_environment(), story)

        set_calls = [
            _cmd(c)
            for c in mock_exec.call_args_list
            if _cmd(c)[0] == "tmux" and _cmd(c)[1] == "set-option"
        ]
        assert len(set_calls) == 2
        session = _tmux_session_name(story)
        for args in set_calls:
            assert args[args.index("-t") + 1] == session
        options = {(args[-2], args[-1]) for args in set_calls}
        assert ("mouse", "on") in options
        assert ("history-limit", "100000") in options

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_windows_terminal_tab_attaches_to_session(
        self, mock_exec: AsyncMock
    ) -> None:
        """The final call opens a Windows Terminal tab attaching to the session."""
        mock_exec.return_value = _make_successful_process()

        runner = ClaudeCodeRunner(_make_config())
        story = _make_story()
        await runner.invoke(_make_environment(), story)

        args = _cmd(mock_exec.call_args_list[-1])
        session = _tmux_session_name(story)
        assert "wt.exe" in args[0]
        assert "nt" in args  # new tab
        assert "wsl.exe" in args
        assert "tmux" in args
        assert "attach" in args
        assert args[args.index("-t") + 1] == session

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_correct_command_order(self, mock_exec: AsyncMock) -> None:
        """Commands: kill-session -> new-session -> set-option(x2) -> wt.exe."""
        mock_exec.return_value = _make_successful_process()

        runner = ClaudeCodeRunner(_make_config())
        await runner.invoke(_make_environment(), _make_story())

        # 1 kill + 1 new-session + 2 set-option + 1 wt.exe == 5
        assert mock_exec.call_count == 5
        argvs = [_cmd(c) for c in mock_exec.call_args_list]

        assert (argvs[0][0], argvs[0][1]) == ("tmux", "kill-session")
        assert (argvs[1][0], argvs[1][1]) == ("tmux", "new-session")
        assert (argvs[2][0], argvs[2][1]) == ("tmux", "set-option")
        assert (argvs[3][0], argvs[3][1]) == ("tmux", "set-option")
        assert "wt.exe" in argvs[4][0]

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_no_xdotool_invoked(self, mock_exec: AsyncMock) -> None:
        """The rewritten flow must never shell out to xdotool anymore."""
        mock_exec.return_value = _make_successful_process()

        runner = ClaudeCodeRunner(_make_config())
        await runner.invoke(_make_environment(), _make_story())

        assert all(_cmd(c)[0] != "xdotool" for c in mock_exec.call_args_list)


class TestTmuxFailure:
    """Tests error handling for the tmux launch step."""

    @pytest.fixture(autouse=True)
    def _force_wsl_terminal(self):
        """Pin WSL + resolvable Windows Terminal (see TestInvokeCommandSequence)
        so the wt.exe-tolerance test exercises the terminal-tab path on any host.
        """
        with (
            patch("backend.osenv.os_kind", return_value="wsl"),
            patch(
                "backend.ticket_ingestion.terminal_launch.shutil.which",
                side_effect=lambda cmd: cmd,
            ),
            patch(
                "backend.ticket_ingestion.terminal_launch." "wsl_interop_available",
                return_value=True,
            ),
        ):
            yield

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_runtime_error_when_new_session_fails(
        self, mock_exec: AsyncMock
    ) -> None:
        """A non-zero tmux new-session exit raises RuntimeError."""
        kill = _make_successful_process()  # kill-session
        new_session = _make_failed_process(stderr=b"duplicate session: sc-12345")
        mock_exec.side_effect = [kill, new_session]

        runner = ClaudeCodeRunner(_make_config())

        with pytest.raises(RuntimeError, match="tmux new-session failed"):
            await runner.invoke(_make_environment(), _make_story())

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_wt_failure_is_tolerated(self, mock_exec: AsyncMock) -> None:
        """A wt.exe failure is logged, not raised: the tmux session survives."""

        def _side_effect(*args, **kwargs):
            if args and "wt.exe" in str(args[0]):
                return _make_failed_process(stderr=b"wt.exe not found")
            return _make_successful_process()

        mock_exec.side_effect = _side_effect

        runner = ClaudeCodeRunner(_make_config())
        # Should complete without raising even though the terminal tab failed.
        await runner.invoke(_make_environment(), _make_story())

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_set_option_failure_is_tolerated(self, mock_exec: AsyncMock) -> None:
        """A failing set-option is logged as a warning, not raised."""

        def _side_effect(*args, **kwargs):
            if args and args[0] == "tmux" and args[1] == "set-option":
                return _make_failed_process(stderr=b"unknown option")
            return _make_successful_process()

        mock_exec.side_effect = _side_effect

        runner = ClaudeCodeRunner(_make_config())
        await runner.invoke(_make_environment(), _make_story())

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_terminal_spawn_exception_is_tolerated(
        self, mock_exec: AsyncMock, caplog
    ) -> None:
        """Opening the terminal tab must never fail the invoke, regardless of
        how the spawn blows up on a given OS. A missing emulator raises
        FileNotFoundError; an event loop without subprocess support raises
        NotImplementedError. Both must be swallowed — and the user must be told
        how to recover (``tmux attach -t <session>``)."""
        story = _make_story()
        session = _tmux_session_name(story)
        for exc in (FileNotFoundError("wt.exe"), NotImplementedError()):

            def _side_effect(*args, **kwargs):
                if args and "wt.exe" in str(args[0]):
                    raise exc
                return _make_successful_process()

            mock_exec.side_effect = _side_effect

            runner = ClaudeCodeRunner(_make_config())
            caplog.clear()
            with caplog.at_level(
                "WARNING", logger="backend.ticket_ingestion.claude_runner"
            ):
                # Must complete without raising even though the tab spawn threw.
                await runner.invoke(_make_environment(), story)
            # The recovery hint must survive: a warning that names the manual
            # ``tmux attach -t <session>`` fallback (so the guidance can't
            # silently regress into a bare "failed" log).
            warnings = [
                r.getMessage() for r in caplog.records if r.levelname == "WARNING"
            ]
            assert any(
                "tmux attach -t" in m and session in m for m in warnings
            ), warnings

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.claude_runner.asyncio.create_subprocess_exec")
    async def test_non_terminal_spawn_exception_propagates(
        self, mock_exec: AsyncMock
    ) -> None:
        """The broad terminal-tab tolerance must NOT swallow every subprocess
        failure: a spawn error on the tmux new-session step still fails the
        invoke (surfaced as a RuntimeError), so a genuinely dead launch isn't
        hidden."""

        def _side_effect(*args, **kwargs):
            if args and args[0] == "tmux" and args[1] == "new-session":
                raise FileNotFoundError("tmux")
            return _make_successful_process()

        mock_exec.side_effect = _side_effect

        runner = ClaudeCodeRunner(_make_config())
        with pytest.raises(RuntimeError):
            await runner.invoke(_make_environment(), _make_story())


class TestPromptContent:
    """Tests that the prompt passed to Claude contains all required sections."""

    def test_prompt_contains_story_title(self) -> None:
        """The prompt file contains the story title."""
        runner = ClaudeCodeRunner(_make_config())
        story = _make_story()
        prompt = runner._build_prompt(story)
        assert story.name in prompt

    def test_prompt_contains_description(self) -> None:
        """The prompt file contains the full story description."""
        runner = ClaudeCodeRunner(_make_config())
        story = _make_story()
        prompt = runner._build_prompt(story)
        assert story.description in prompt

    def test_prompt_contains_all_acceptance_criteria(self) -> None:
        """The prompt file contains all acceptance criteria."""
        runner = ClaudeCodeRunner(_make_config())
        story = _make_story()
        prompt = runner._build_prompt(story)
        for criterion in story.acceptance_criteria:
            assert criterion in prompt

    def test_prompt_contains_shortcut_url(self) -> None:
        """The prompt file references the Shortcut story URL."""
        runner = ClaudeCodeRunner(_make_config())
        story = _make_story()
        prompt = runner._build_prompt(story)
        assert story.app_url in prompt

    def test_prompt_contains_ticket_instructions(self) -> None:
        """The prompt opens with the follow-the-ticket-requirements guidance."""
        runner = ClaudeCodeRunner(_make_config())
        prompt = runner._build_prompt(_make_story())
        assert "Follow the ticket requirements" in prompt

    def test_prompt_contains_supplemental_context_when_provided(self) -> None:
        """The prompt includes supplemental context when provided."""
        runner = ClaudeCodeRunner(_make_config())
        story = _make_story()
        supplemental = "Use the existing auth middleware in src/middleware/auth.py"
        prompt = runner._build_prompt(story, supplemental_context=supplemental)
        assert supplemental in prompt
        assert "Supplemental Context" in prompt

    def test_prompt_omits_supplemental_section_when_absent(self) -> None:
        """No supplemental section is emitted when none is provided."""
        runner = ClaudeCodeRunner(_make_config())
        prompt = runner._build_prompt(_make_story())
        assert "Supplemental Context" not in prompt
