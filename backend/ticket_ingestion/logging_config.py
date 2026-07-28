"""Logging configuration."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend.ticket_ingestion.config import PipelineConfig

PACKAGE_LOGGER_NAME = "backend.ticket_ingestion"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _log_max_bytes() -> int:
    """Rolling-window size for the pipeline log so it can't grow unbounded.
    Keeps the last ~2 MB (current file ≤ this, plus one rollover backup) — big
    enough that DEBUG-level ingestion history survives long enough to read in
    Settings → System logs (whose tail reads the last 256 KB). Override with
    MINDFLOCK_PIPELINE_LOG_MAX_BYTES."""
    try:
        return int(os.environ.get("MINDFLOCK_PIPELINE_LOG_MAX_BYTES", 2 * 1024 * 1024))
    except (TypeError, ValueError):
        return 2 * 1024 * 1024


def setup_logging(config: PipelineConfig) -> None:
    log_path = Path(config.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    logger.setLevel(config.log_level.upper())
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(LOG_FORMAT, datefmt=_DATE_FORMAT)

    # Rolling window: cap the pipeline log at ~50 KB (one rollover backup) so a
    # long-running pipeline can't grow it unbounded.
    file_handler = RotatingFileHandler(
        log_path, maxBytes=_log_max_bytes(), backupCount=1, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
