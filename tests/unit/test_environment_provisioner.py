"""Unit tests for the EnvironmentProvisioner.

Tests branch naming conventions, directory naming conventions,
and correct subprocess invocations for git, uv, and cursor commands.

All external effects (git / uv / cursor) are mocked; nothing real is
launched. Filesystem writes are confined to a pytest tmp_path workspace.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion.models import Ticket
from backend.ticket_ingestion.provisioner import (
    EnvironmentProvisioner,
    ProvisioningError,
)
from tests._factories import make_ticket
from backend.web.core import ide_launch as _ide_launch


@pytest.fixture(autouse=True)
def ide_launch_calls(monkeypatch):
    """Stub the unified IDE launcher (provision() routes through it now) so no
    real editor is ever opened; records the launched paths."""
    calls: list = []
    monkeypatch.setattr(
        _ide_launch,
        "launch_ide",
        lambda path, argv=None: calls.append(str(path)),
    )
    return calls


def _make_config(workspace_dir: Path) -> PipelineConfig:
    """Helper to create a minimal PipelineConfig for testing."""
    return PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut",
            api_token="test-token",
            member_id="member-123",
        ),
        repo_url="git@github.com:org/example-bot.git",
        workspace_dir=workspace_dir,
        min_description_length=20,
        log_file=Path("/tmp/pipeline.log"),
        log_level="INFO",
    )


def _make_story(story_id: int = 12345, name: str = "Test Story") -> Ticket:
    """Helper to create a test Ticket."""
    return make_ticket(
        id=story_id,
        name=name,
        description="A valid description that is long enough for validation",
        acceptance_criteria=["WHEN user clicks submit THEN form is saved"],
        created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


def _expected_branch(story: Ticket) -> str:
    """Mirror provisioner._branch_name_for for the standard 'Test Story' name."""
    # _slugify("Test Story") -> "test-story"
    return f"feature/sc-{story.id}/test-story"


def _expected_directory(workspace_dir: Path, story: Ticket) -> Path:
    return workspace_dir / _expected_branch(story).replace("/", "-")


def _make_successful_process(stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    """Create a mock process that completes successfully (returncode=0)."""
    process = AsyncMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(stdout, stderr))
    return process


def _make_failed_process(stderr: bytes = b"error occurred") -> AsyncMock:
    """Create a mock process that fails (returncode=1)."""
    process = AsyncMock()
    process.returncode = 1
    process.communicate = AsyncMock(return_value=(b"", stderr))
    return process


def _clone_process_for(directory: Path) -> AsyncMock:
    """A successful `git clone` mock that materializes the `.clone-tmp` dir.

    The real provisioner clones into ``<directory>.clone-tmp`` and then renames
    it into place, so the mock must create that directory (as git would) for the
    subsequent rename to succeed. The tmp dir is created empty, so no optional
    files (.pre-commit-config.yaml, auto_fix_precommit_hook.py) are present.
    """
    tmp_dir = directory.with_name(directory.name + ".clone-tmp")

    async def _create_and_return(*_args, **_kwargs):
        tmp_dir.mkdir(parents=True, exist_ok=True)
        return (b"", b"")

    process = AsyncMock()
    process.returncode = 0
    process.communicate = AsyncMock(side_effect=_create_and_return)
    return process


def _make_exec_mock(directory: Path) -> AsyncMock:
    """Build a create_subprocess_exec mock whose clone materializes the tmp dir.

    With an empty clone (no pyproject.toml / uv.lock, so no auto-detected setup
    commands) and no cache seeds configured, the provision flow issues exactly
    two exec calls in order:
      1. git clone --depth=1 <repo_url> <dir>.clone-tmp
      2. git checkout -B <branch> (cwd=<dir>)
    Setup commands (uv sync etc.) run through create_subprocess_shell instead,
    and the IDE launch goes through ide_launch.launch_ide (stubbed by the
    autouse ``ide_launch_calls`` fixture).
    """
    mock_exec = AsyncMock()
    mock_exec.side_effect = [
        _clone_process_for(directory),  # git clone
        _make_successful_process(),  # git checkout -B
    ]
    return mock_exec


class TestBranchNamingConvention:
    """Branch naming convention: feature/sc-<story_id>/<slug>."""

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_branch_name_format(
        self, mock_exec: AsyncMock, tmp_path: Path
    ) -> None:
        story = _make_story(story_id=42)
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = _make_exec_mock(directory).side_effect

        provisioner = EnvironmentProvisioner(_make_config(tmp_path))
        result = await provisioner.provision(story)

        assert result.branch_name == "feature/sc-42/test-story"

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_branch_name_with_large_id(
        self, mock_exec: AsyncMock, tmp_path: Path
    ) -> None:
        story = _make_story(story_id=999999)
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = _make_exec_mock(directory).side_effect

        provisioner = EnvironmentProvisioner(_make_config(tmp_path))
        result = await provisioner.provision(story)

        assert result.branch_name == "feature/sc-999999/test-story"


class TestDirectoryNamingConvention:
    """Directory convention: <workspace_dir>/<branch-with-slashes-as-dashes>/."""

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_directory_format(self, mock_exec: AsyncMock, tmp_path: Path) -> None:
        story = _make_story(story_id=777)
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = _make_exec_mock(directory).side_effect

        provisioner = EnvironmentProvisioner(_make_config(tmp_path))
        result = await provisioner.provision(story)

        assert result.directory == tmp_path / "feature-sc-777-test-story"

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_directory_uses_configured_workspace_dir(
        self, mock_exec: AsyncMock, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspaces"
        story = _make_story(story_id=100)
        directory = _expected_directory(workspace, story)
        mock_exec.side_effect = _make_exec_mock(directory).side_effect

        provisioner = EnvironmentProvisioner(_make_config(workspace))
        result = await provisioner.provision(story)

        assert result.directory == workspace / "feature-sc-100-test-story"
        assert result.directory.parent == workspace


class TestCursorWindowId:
    """provision() reports a fixed cursor_window_id of 0 (no window polling)."""

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_window_id_is_zero(
        self, mock_exec: AsyncMock, tmp_path: Path
    ) -> None:
        story = _make_story(story_id=1)
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = _make_exec_mock(directory).side_effect

        provisioner = EnvironmentProvisioner(_make_config(tmp_path))
        result = await provisioner.provision(story)

        assert result.cursor_window_id == 0


class TestGitCloneInvocation:
    """Correct git clone subprocess invocation."""

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_git_clone_called_with_correct_args(
        self, mock_exec: AsyncMock, tmp_path: Path
    ) -> None:
        story = _make_story(story_id=500)
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = _make_exec_mock(directory).side_effect

        provisioner = EnvironmentProvisioner(_make_config(tmp_path))
        await provisioner.provision(story)

        first_call = mock_exec.call_args_list[0]
        args = first_call[0]
        tmp_dir = directory.with_name(directory.name + ".clone-tmp")
        assert args[0] == "git"
        assert args[1] == "clone"
        assert args[2] == "--depth=1"
        assert args[3] == "git@github.com:org/example-bot.git"
        assert args[4] == str(tmp_dir)


class TestSetupCommandInvocation:
    """Setup commands run through the shell in the workspace directory."""

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_shell")
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_uv_sync_auto_detected_for_python_project(
        self, mock_exec: AsyncMock, mock_shell: AsyncMock, tmp_path: Path
    ) -> None:
        """A cloned uv project auto-runs `uv sync --all-groups` in the workspace."""
        story = _make_story(story_id=200)
        directory = _expected_directory(tmp_path, story)
        mock_shell.return_value = _make_successful_process()

        # A "clone" that produces a Python project so auto-detection fires.
        tmp_dir = directory.with_name(directory.name + ".clone-tmp")

        async def _clone_with_pyproject(*_args, **_kwargs):
            tmp_dir.mkdir(parents=True, exist_ok=True)
            (tmp_dir / "pyproject.toml").write_text("[project]\nname='x'\n")
            return (b"", b"")

        clone = AsyncMock()
        clone.returncode = 0
        clone.communicate = AsyncMock(side_effect=_clone_with_pyproject)
        mock_exec.side_effect = [
            clone,  # git clone
            _make_successful_process(),  # git checkout -B
            _make_successful_process(),  # cursor launch
        ]

        provisioner = EnvironmentProvisioner(_make_config(tmp_path))
        await provisioner.provision(story)

        shell_call = mock_shell.call_args_list[0]
        assert shell_call[0][0] == "uv sync --all-groups"
        assert shell_call[1]["cwd"] == str(directory)

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_shell")
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_configured_setup_commands_override_auto_detection(
        self, mock_exec: AsyncMock, mock_shell: AsyncMock, tmp_path: Path
    ) -> None:
        story = _make_story(story_id=201)
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = _make_exec_mock(directory).side_effect
        mock_shell.return_value = _make_successful_process()

        config = _make_config(tmp_path)
        config.setup_commands = ["make deps", "make hooks"]
        provisioner = EnvironmentProvisioner(config)
        await provisioner.provision(story)

        commands = [c[0][0] for c in mock_shell.call_args_list]
        assert commands == ["make deps", "make hooks"]


class TestGitCheckoutInvocation:
    """Correct git checkout -B subprocess invocation."""

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_git_checkout_called_with_correct_branch(
        self, mock_exec: AsyncMock, tmp_path: Path
    ) -> None:
        story = _make_story(story_id=300)
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = _make_exec_mock(directory).side_effect

        provisioner = EnvironmentProvisioner(_make_config(tmp_path))
        await provisioner.provision(story)

        second_call = mock_exec.call_args_list[1]
        args = second_call[0]
        kwargs = second_call[1]
        assert args[0] == "git"
        assert args[1] == "checkout"
        assert args[2] == "-B"
        assert args[3] == "feature/sc-300/test-story"
        assert kwargs["cwd"] == str(directory)


class TestCursorLaunchInvocation:
    """Correct IDE-launcher invocation (routed through ide_launch.launch_ide)."""

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_ide_launched_with_correct_directory(
        self, mock_exec: AsyncMock, tmp_path: Path, ide_launch_calls: list
    ) -> None:
        story = _make_story(story_id=400)
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = _make_exec_mock(directory).side_effect

        provisioner = EnvironmentProvisioner(_make_config(tmp_path))
        await provisioner.provision(story)

        assert ide_launch_calls == [str(directory)]


class TestProvisioningErrors:
    """ProvisioningError raised on subprocess failures."""

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_git_clone_failure_raises_provisioning_error(
        self, mock_exec: AsyncMock, tmp_path: Path
    ) -> None:
        mock_exec.return_value = _make_failed_process(
            stderr=b"fatal: repository not found"
        )

        provisioner = EnvironmentProvisioner(_make_config(tmp_path))
        story = _make_story()

        with pytest.raises(ProvisioningError, match="Git clone failed"):
            await provisioner.provision(story)

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_shell")
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_setup_command_failure_continues(
        self, mock_exec: AsyncMock, mock_shell: AsyncMock, tmp_path: Path
    ) -> None:
        """A failing setup command warns and continues; provisioning completes."""
        story = _make_story()
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = _make_exec_mock(directory).side_effect
        mock_shell.return_value = _make_failed_process(
            stderr=b"error: could not resolve dependencies"
        )

        config = _make_config(tmp_path)
        config.setup_commands = ["uv sync --all-groups"]
        provisioner = EnvironmentProvisioner(config)

        result = await provisioner.provision(story)

        assert result.directory == directory

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_git_checkout_failure_raises_provisioning_error(
        self, mock_exec: AsyncMock, tmp_path: Path
    ) -> None:
        story = _make_story()
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = [
            _clone_process_for(directory),  # git clone
            _make_failed_process(stderr=b"fatal: branch error"),  # git checkout
        ]

        provisioner = EnvironmentProvisioner(_make_config(tmp_path))

        with pytest.raises(ProvisioningError, match="git checkout failed"):
            await provisioner.provision(story)

    @pytest.mark.asyncio
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_cursor_launch_failure_raises_provisioning_error(
        self, mock_exec: AsyncMock, tmp_path: Path, monkeypatch
    ) -> None:
        story = _make_story()
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = [
            _clone_process_for(directory),  # git clone
            _make_successful_process(),  # git checkout -B
        ]

        def _boom(path, argv=None):
            raise _ide_launch.IdeLaunchError("`cursor` is not on PATH")

        monkeypatch.setattr(_ide_launch, "launch_ide", _boom)
        provisioner = EnvironmentProvisioner(_make_config(tmp_path))

        with pytest.raises(ProvisioningError, match="launch failed"):
            await provisioner.provision(story)
