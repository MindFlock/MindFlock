"""Unit tests for logging configuration."""

import logging
from pathlib import Path


from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig
from backend.ticket_ingestion.logging_config import (
    PACKAGE_LOGGER_NAME,
    setup_logging,
)


def _make_config(tmp_path: Path, log_level: str = "INFO") -> PipelineConfig:
    """Create a PipelineConfig with a temporary log file path."""
    return PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut",
            api_token="test-token",
            member_id="member-123",
        ),
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "workspaces",
        min_description_length=20,
        log_file=tmp_path / "logs" / "pipeline.log",
        log_level=log_level,
    )


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_creates_log_directory(self, tmp_path: Path) -> None:
        """Log directory is created if it doesn't exist."""
        config = _make_config(tmp_path)
        assert not config.log_file.parent.exists()

        setup_logging(config)

        assert config.log_file.parent.exists()

    def test_creates_nested_log_directory(self, tmp_path: Path) -> None:
        """Nested log directories are created with parents=True."""
        config = PipelineConfig(
            ticketing=TicketProviderConfig(
                provider="shortcut",
                api_token="test-token",
                member_id="member-123",
            ),
            repo_url="git@github.com:org/repo.git",
            workspace_dir=tmp_path / "workspaces",
            min_description_length=20,
            log_file=tmp_path / "deep" / "nested" / "logs" / "pipeline.log",
            log_level="INFO",
        )

        setup_logging(config)

        assert config.log_file.parent.exists()

    def test_configures_package_logger(self, tmp_path: Path) -> None:
        """The package-level logger is configured with the correct level."""
        config = _make_config(tmp_path, log_level="DEBUG")

        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        assert logger.level == logging.DEBUG

    def test_sets_info_level(self, tmp_path: Path) -> None:
        """INFO log level is set correctly."""
        config = _make_config(tmp_path, log_level="INFO")

        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        assert logger.level == logging.INFO

    def test_sets_warning_level(self, tmp_path: Path) -> None:
        """WARNING log level is set correctly."""
        config = _make_config(tmp_path, log_level="WARNING")

        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        assert logger.level == logging.WARNING

    def test_case_insensitive_log_level(self, tmp_path: Path) -> None:
        """Log level string is case-insensitive."""
        config = _make_config(tmp_path, log_level="debug")

        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        assert logger.level == logging.DEBUG

    def test_adds_file_handler(self, tmp_path: Path) -> None:
        """A FileHandler writing to config.log_file is added."""
        config = _make_config(tmp_path)

        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        file_handlers = [
            h for h in logger.handlers if isinstance(h, logging.FileHandler)
        ]
        assert len(file_handlers) == 1
        assert Path(file_handlers[0].baseFilename) == config.log_file.resolve()

    def test_adds_stream_handler(self, tmp_path: Path) -> None:
        """A StreamHandler for console output is added."""
        config = _make_config(tmp_path)

        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        stream_handlers = [
            h
            for h in logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        assert len(stream_handlers) == 1

    def test_writes_to_log_file(self, tmp_path: Path) -> None:
        """Log messages are written to the configured log file."""
        config = _make_config(tmp_path)

        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        logger.info("Test message")

        # Flush handlers
        for handler in logger.handlers:
            handler.flush()

        log_content = config.log_file.read_text()
        assert "Test message" in log_content

    def test_log_format_includes_timestamp(self, tmp_path: Path) -> None:
        """Log messages include a timestamp."""
        config = _make_config(tmp_path)

        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        logger.info("Timestamp test")

        for handler in logger.handlers:
            handler.flush()

        log_content = config.log_file.read_text()
        # Timestamp format: 2025-01-15T10:30:00
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", log_content)

    def test_log_format_includes_level(self, tmp_path: Path) -> None:
        """Log messages include the log level."""
        config = _make_config(tmp_path)

        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        logger.warning("Level test")

        for handler in logger.handlers:
            handler.flush()

        log_content = config.log_file.read_text()
        assert "[WARNING]" in log_content

    def test_log_format_includes_logger_name(self, tmp_path: Path) -> None:
        """Log messages include the logger name."""
        config = _make_config(tmp_path)

        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        logger.info("Name test")

        for handler in logger.handlers:
            handler.flush()

        log_content = config.log_file.read_text()
        assert PACKAGE_LOGGER_NAME in log_content

    def test_child_logger_inherits_config(self, tmp_path: Path) -> None:
        """Child loggers (e.g., per-module) inherit the configuration."""
        config = _make_config(tmp_path)

        setup_logging(config)

        child_logger = logging.getLogger(f"{PACKAGE_LOGGER_NAME}.webhook")
        child_logger.info("Child logger message")

        for handler in logging.getLogger(PACKAGE_LOGGER_NAME).handlers:
            handler.flush()

        log_content = config.log_file.read_text()
        assert "Child logger message" in log_content
        assert f"{PACKAGE_LOGGER_NAME}.webhook" in log_content

    def test_no_duplicate_handlers_on_repeated_calls(self, tmp_path: Path) -> None:
        """Calling setup_logging multiple times does not add duplicate handlers."""
        config = _make_config(tmp_path)

        setup_logging(config)
        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        assert len(logger.handlers) == 2  # One file, one stream

    def test_file_handler_is_rolling_and_bounded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The pipeline log rolls over so it can't grow unbounded: the active
        file stays under the cap and at most one backup is kept."""
        from logging.handlers import RotatingFileHandler

        monkeypatch.setenv("MINDFLOCK_PIPELINE_LOG_MAX_BYTES", "4096")
        config = _make_config(tmp_path)
        setup_logging(config)

        logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        fh = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))
        assert fh.maxBytes == 4096 and fh.backupCount == 1

        for i in range(2000):
            logger.info("log line %d padding-padding-padding-padding", i)
        for handler in logger.handlers:
            handler.flush()

        # Active file capped; exactly one rollover backup (no unbounded growth).
        assert config.log_file.stat().st_size <= 4096
        siblings = list(config.log_file.parent.glob("pipeline.log*"))
        assert config.log_file.with_suffix(".log.1") in siblings
        assert len(siblings) == 2  # pipeline.log + pipeline.log.1 only

    def test_story_id_in_log_message(self, tmp_path: Path) -> None:
        """Story ID can be included in log messages for per-story tracking."""
        config = _make_config(tmp_path)

        setup_logging(config)

        logger = logging.getLogger(f"{PACKAGE_LOGGER_NAME}.orchestrator")
        story_id = 12345
        logger.info("Processing story_id=%d: validation passed", story_id)

        for handler in logging.getLogger(PACKAGE_LOGGER_NAME).handlers:
            handler.flush()

        log_content = config.log_file.read_text()
        assert "story_id=12345" in log_content
