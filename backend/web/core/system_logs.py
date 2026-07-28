"""System-log tails for Settings → System logs.

Which log files are worth surfacing (the server's own log always, the
ingestion pipeline log when present) and a bounded tail reader — only ever the
last ``_LOG_TAIL_MAX`` bytes, starting on a clean line boundary.

Split out of ``backend.web.server`` (which re-imports these names — the
``/api/logs`` route and tests reference them through the server namespace).
"""

from __future__ import annotations

import os
from pathlib import Path

from backend import log

_LOG_TAIL_MAX = 256 * 1024  # only ever read/return the last 256 KB of a log


def _resolve_repo_root() -> Path:
    """The directory the ingestion pipeline runs in — where ``config.toml`` and
    its ``logs/`` live. Same resolution the ingestion addon uses to launch the
    subprocess (``MINDFLOCK_REPO_ROOT`` env → nearest ancestor with
    ``config.toml`` → cwd), because THIS server's cwd is not guaranteed to
    match the pipeline's — which is exactly why a hard-coded relative
    ``logs/pipeline.log`` never resolved and the ingestion log went missing."""
    env = (os.environ.get("MINDFLOCK_REPO_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "config.toml").is_file():
            return parent
    return Path.cwd()


def _log_sources() -> list:
    """Log files worth surfacing in Settings → System logs: the server's own
    log always, plus the ingestion pipeline's logs when they exist. The pipeline
    runs as a subprocess from the repo root, so its logs live under
    ``<repo root>/logs/`` — resolved here rather than relative to this server's
    cwd. Both the raw stdout/stderr capture (what the sidebar tails) and the
    structured pipeline log are offered so nothing about a run is hidden."""
    sources = [{"name": "server", "label": "Server", "path": str(log.logFileName)}]
    root = _resolve_repo_root()
    candidates = [
        ("ingestion", "Ingestion pipeline", root / "logs" / "ticket-ingestion.log"),
        ("pipeline", "Ingestion (structured)", root / "logs" / "pipeline.log"),
    ]
    for name, label, path in candidates:
        try:
            if path.is_file():
                sources.append({"name": name, "label": label, "path": str(path)})
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
