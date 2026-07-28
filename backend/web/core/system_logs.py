"""System-log tails for Settings → System logs.

Which log files are worth surfacing (the server's own log always, the
ingestion pipeline log when present) and a bounded tail reader — only ever the
last ``_LOG_TAIL_MAX`` bytes, starting on a clean line boundary.

Split out of ``backend.web.server`` (which re-imports these names — the
``/api/logs`` route and tests reference them through the server namespace).
"""

from __future__ import annotations

from pathlib import Path

from backend import log

_LOG_TAIL_MAX = 256 * 1024  # only ever read/return the last 256 KB of a log


def _log_sources() -> list:
    """Log files worth surfacing: the server's own log always, plus the
    ingestion pipeline log when it exists (default ``./logs/pipeline.log``)."""
    sources = [{"name": "server", "label": "Server", "path": str(log.logFileName)}]
    ing = Path("logs/pipeline.log")
    try:
        if ing.exists():
            sources.append(
                {
                    "name": "ingestion",
                    "label": "Ingestion pipeline",
                    "path": str(ing.resolve()),
                }
            )
    except OSError:
        pass
    return sources


def _read_log_tail(path: Path, max_bytes: int = _LOG_TAIL_MAX):
    """Return ``(text, total_size)`` reading at most the last ``max_bytes`` and
    starting on a clean line boundary when the file was truncated."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
    return text, size
