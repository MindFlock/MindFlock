"""Every ticket-id log line must survive a STRING id (Jira / Linear / Asana).

``Ticket.id`` is the provider-native id: an int for Shortcut/GitHub, but a
string for Jira (``PROJ-42``), Linear (``ENG-9``) and Asana (a numeric-looking
gid). A ``%d`` placeholder fed a string makes ``logging`` raise
``TypeError: %d format: a real number is required, not str``, print
``--- Logging error ---`` plus a traceback to stderr, and then DROP the record
— so a Jira user's pipeline.log collected tracebacks where the provisioning
narrative belonged.

These tests emit each affected record through the real logging machinery with a
string id and assert on the FORMATTED text, so a reintroduced ``%d`` fails here
instead of silently shredding the log.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from backend.config import ide as _ide
from backend.ticket_ingestion import claude_runner as _claude_runner
from backend.ticket_ingestion.claude_runner import ClaudeCodeRunner
from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion.models import Attachment, Ticket
from backend.ticket_ingestion.provisioner import EnvironmentProvisioner
from backend.web.core import ide_launch as _ide_launch
from tests._factories import make_ticket

# Real-shaped string ids: a Jira key, a Linear identifier, and an Asana gid
# (numeric-looking but still a str — the case a naive `%d` almost survives).
_JIRA_ID = "PROJ-42"
_LINEAR_ID = "ENG-9"
_ASANA_GID = "1207894561234567"


class _StrictHandler(logging.Handler):
    """Captures FORMATTED records and refuses to swallow format errors.

    Stock ``logging.Handler.emit`` funnels a bad format string into
    ``handleError``, which prints ``--- Logging error ---`` to stderr and throws
    the record away — the exact silent loss under test. Formatting eagerly here,
    outside any ``try``, turns that loss into a ``TypeError`` at the log call
    site and therefore a test failure.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))

    def text(self) -> str:
        return "\n".join(self.messages)


_WATCHED_LOGGERS = (
    "backend.ticket_ingestion.provisioner",
    "backend.ticket_ingestion.claude_runner",
)


@pytest.fixture
def strict_logs():
    """Attach a :class:`_StrictHandler` to the loggers whose records we assert on."""
    handler = _StrictHandler()
    loggers = [logging.getLogger(name) for name in _WATCHED_LOGGERS]
    saved = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for lg, level in saved:
            lg.removeHandler(handler)
            lg.setLevel(level)


@pytest.fixture(autouse=True)
def _no_real_ide(monkeypatch):
    """provision() routes the editor launch through ide_launch; stub it out."""
    monkeypatch.setattr(_ide_launch, "launch_ide", lambda path, argv=None: None)


def _config(workspace_dir: Path) -> PipelineConfig:
    return PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="jira",
            api_token="test-token",
            member_id="557058:abc",
        ),
        repo_url="git@github.com:org/example-bot.git",
        workspace_dir=workspace_dir,
        min_description_length=20,
        log_file=Path("/tmp/pipeline.log"),
        log_level="INFO",
    )


def _string_id_ticket(ticket_id: str, slug_prefix: str, provider: str) -> Ticket:
    return make_ticket(
        id=ticket_id,
        slug=f"{slug_prefix}-{ticket_id}",
        provider=provider,
        name="Test Story",
        description="A valid description that is long enough for validation",
        acceptance_criteria=["WHEN user clicks submit THEN form is saved"],
        created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


def _expected_directory(workspace_dir: Path, story: Ticket) -> Path:
    # Mirrors provisioner._branch_name_for for the "Test Story" name.
    return workspace_dir / f"feature-{story.slug}-test-story"


def _ok_process(stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    process = AsyncMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(stdout, stderr))
    return process


def _failed_process(stderr: bytes = b"boom") -> AsyncMock:
    process = AsyncMock()
    process.returncode = 1
    process.communicate = AsyncMock(return_value=(b"", stderr))
    return process


def _clone_process_for(
    directory: Path, *, extra_files: dict | None = None
) -> AsyncMock:
    """A successful `git clone` mock that materializes the `.clone-tmp` dir.

    The provisioner clones into ``<directory>.clone-tmp`` then renames it into
    place, so the mock must create that directory for the rename to succeed.
    ``extra_files`` seeds files the clone would have contained.
    """
    tmp_dir = directory.with_name(directory.name + ".clone-tmp")

    async def _create_and_return(*_args, **_kwargs):
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for name, body in (extra_files or {}).items():
            (tmp_dir / name).write_text(body)
        return (b"", b"")

    process = AsyncMock()
    process.returncode = 0
    process.communicate = AsyncMock(side_effect=_create_and_return)
    return process


# --------------------------------------------------------------------------- #
# EnvironmentProvisioner: the whole provisioning narrative
# --------------------------------------------------------------------------- #
class TestProvisionerLogsStringIds:
    @pytest.mark.parametrize(
        ("ticket_id", "slug_prefix", "provider"),
        [
            (_JIRA_ID, "jira", "jira"),
            (_LINEAR_ID, "lin", "linear"),
            (_ASANA_GID, "asana", "asana"),
        ],
    )
    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_provision_narrative_formats_string_id(
        self,
        mock_exec: AsyncMock,
        ticket_id: str,
        slug_prefix: str,
        provider: str,
        tmp_path: Path,
        strict_logs: _StrictHandler,
    ) -> None:
        """The happy-path provisioning lines all render the string id verbatim."""
        story = _string_id_ticket(ticket_id, slug_prefix, provider)
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = [
            _clone_process_for(directory),  # git clone
            _ok_process(),  # git checkout -B
        ]

        await EnvironmentProvisioner(_config(tmp_path)).provision(story)

        branch = f"feature/{story.slug}/test-story"
        text = strict_logs.text()
        assert (
            f"Provisioning workspace for story {ticket_id} at {directory} "
            f"(branch: {branch})" in text
        )
        assert (
            f"Cloning git@github.com:org/example-bot.git for story {ticket_id} "
            f"into {directory}" in text
        )
        assert f"Creating branch {branch} for story {ticket_id}" in text
        assert f"Opening {_ide.ide_name()} for story {ticket_id}" in text
        assert f"skipping log wrapper install for story {ticket_id}" in text
        assert f"Workspace ready for story {ticket_id}." in text

    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_reprovision_over_existing_workspace_formats_string_id(
        self,
        mock_exec: AsyncMock,
        tmp_path: Path,
        strict_logs: _StrictHandler,
    ) -> None:
        """The "Removing existing workspace" line only fires on a re-provision."""
        story = _string_id_ticket(_JIRA_ID, "jira", "jira")
        directory = _expected_directory(tmp_path, story)
        directory.mkdir(parents=True)
        (directory / "stale.txt").write_text("left over from a previous run")
        mock_exec.side_effect = [
            _clone_process_for(directory),  # git clone
            _ok_process(),  # git checkout -B
        ]

        await EnvironmentProvisioner(_config(tmp_path)).provision(story)

        assert (
            f"Removing existing workspace at {directory} for story {_JIRA_ID}"
            in strict_logs.text()
        )

    @patch("backend.ticket_ingestion.provisioner.asyncio.create_subprocess_exec")
    async def test_precommit_wrapper_failure_formats_string_id(
        self,
        mock_exec: AsyncMock,
        tmp_path: Path,
        strict_logs: _StrictHandler,
    ) -> None:
        """Both pre-commit-wrapper lines (install + failure warning) fire when the
        cloned repo actually ships auto_fix_precommit_hook.py."""
        story = _string_id_ticket(_LINEAR_ID, "lin", "linear")
        directory = _expected_directory(tmp_path, story)
        mock_exec.side_effect = [
            _clone_process_for(
                directory, extra_files={"auto_fix_precommit_hook.py": "print('hi')\n"}
            ),
            _ok_process(),  # git checkout -B
            _failed_process(),  # uv run python auto_fix_precommit_hook.py
        ]

        await EnvironmentProvisioner(_config(tmp_path)).provision(story)

        text = strict_logs.text()
        assert f"Installing pre-commit log wrapper for story {_LINEAR_ID}" in text
        assert (
            f"auto_fix_precommit_hook.py failed for story {_LINEAR_ID} "
            "(continuing): boom" in text
        )


# --------------------------------------------------------------------------- #
# ClaudeCodeRunner._download_attachments (also reached in engine mode via
# SessionRunner._post_start, so these lines hit every MindFlock-mode Jira user)
# --------------------------------------------------------------------------- #
class _FakeContent:
    """Stand-in for ``resp.content`` exposing ``iter_chunked``."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def iter_chunked(self, _size: int):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


class _FakeResp:
    """Stand-in for an aiohttp response used as an async context manager."""

    def __init__(self, status: int = 200, chunks: list[bytes] | None = None) -> None:
        self.status = status
        self.content = _FakeContent(chunks or [b"payload"])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """Stand-in for aiohttp.ClientSession; responses are consumed in order."""

    def __init__(self, responses: list, raises: BaseException | None = None) -> None:
        self._responses = list(responses)
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, _url, **_kwargs):
        if self._raises is not None:
            raise self._raises
        return self._responses.pop(0)


def _runner(tmp_path: Path) -> ClaudeCodeRunner:
    return ClaudeCodeRunner(_config(tmp_path))


def _ticket_with_attachment(ticket_id: str, slug_prefix: str, provider: str) -> Ticket:
    story = _string_id_ticket(ticket_id, slug_prefix, provider)
    story.attachments = [
        Attachment(name="spec.png", url="https://files.example/spec.png")
    ]
    return story


class TestAttachmentLogsStringIds:
    async def test_success_line_formats_string_id(
        self, tmp_path: Path, strict_logs: _StrictHandler
    ) -> None:
        story = _ticket_with_attachment(_JIRA_ID, "jira", "jira")
        session = _FakeSession([_FakeResp(status=200)])

        with patch("aiohttp.ClientSession", return_value=session):
            results = await _runner(tmp_path)._download_attachments(tmp_path, story)

        assert len(results) == 1
        assert (
            f"Downloaded 1 attachment(s) for story {_JIRA_ID} into "
            in strict_logs.text()
        )

    async def test_http_error_line_formats_string_id(
        self, tmp_path: Path, strict_logs: _StrictHandler
    ) -> None:
        story = _ticket_with_attachment(_LINEAR_ID, "lin", "linear")
        session = _FakeSession([_FakeResp(status=403)])

        with patch("aiohttp.ClientSession", return_value=session):
            results = await _runner(tmp_path)._download_attachments(tmp_path, story)

        assert results == []
        assert (
            f"Attachment download failed for story {_LINEAR_ID} "
            "(https://files.example/spec.png): HTTP 403" in strict_logs.text()
        )

    async def test_oversize_line_formats_string_id(
        self, tmp_path: Path, strict_logs: _StrictHandler, monkeypatch
    ) -> None:
        # Shrink the cap instead of downloading 100 MiB.
        monkeypatch.setattr(_claude_runner, "_MAX_ATTACHMENT_BYTES", 4)
        story = _ticket_with_attachment(_ASANA_GID, "asana", "asana")
        session = _FakeSession([_FakeResp(status=200, chunks=[b"0123456789"])])

        with patch("aiohttp.ClientSession", return_value=session):
            results = await _runner(tmp_path)._download_attachments(tmp_path, story)

        assert results == []
        assert (
            f"Attachment https://files.example/spec.png for story {_ASANA_GID} "
            "exceeds 4 bytes; skipping" in strict_logs.text()
        )

    async def test_client_error_line_formats_string_id(
        self, tmp_path: Path, strict_logs: _StrictHandler
    ) -> None:
        story = _ticket_with_attachment(_JIRA_ID, "jira", "jira")
        session = _FakeSession([], raises=aiohttp.ClientError("connection reset"))

        with patch("aiohttp.ClientSession", return_value=session):
            results = await _runner(tmp_path)._download_attachments(tmp_path, story)

        assert results == []
        assert (
            f"Attachment download error for story {_JIRA_ID} "
            "(https://files.example/spec.png): connection reset" in strict_logs.text()
        )


# --------------------------------------------------------------------------- #
# Regression guard: no `%d` may be paired with a ticket id again
# --------------------------------------------------------------------------- #
def test_no_percent_d_ticket_id_format_specifiers_remain() -> None:
    """Grep-style guard over the package so a new ``story %d`` can't sneak back.

    The narrow ``story %d`` / ``ticket %d`` spelling is what actually broke; PR
    and issue numbers are genuine ints and keep their ``%d``.
    """
    package = Path(_claude_runner.__file__).parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            lowered = line.lower()
            if "story %d" in lowered or "ticket %d" in lowered:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == []
