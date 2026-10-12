"""Where the ticket-ingestion pipeline runs, and its config anchored there.

The pipeline keeps ``config.toml``, ``state.json``, ``workspaces/`` and
``logs/`` relative to its cwd (``_STATE_DIR = Path(".")``), and THIS server's
cwd is not guaranteed to match, so every module that reads them (the ingestion
addon, :mod:`pr_review`, :mod:`issue_start`, :mod:`ticket_start`,
:mod:`system_logs`) anchors paths at :func:`resolve_repo_root`. Getting it
wrong splits the processed ledger and re-ingests done tickets.

The old ``Path(__file__).resolve().parents[4]`` only landed on the repo root
for a src-layout dev checkout; for an installed copy (``uv tool install`` →
``…/site-packages/backend/…``) it is the interpreter lib dir.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_repo_root(anchor: str = __file__) -> Path:
    """The pipeline's directory, in this order:

    1. ``MINDFLOCK_REPO_ROOT`` (explicit wins);
    2. the nearest ancestor of ``anchor`` (the caller's ``__file__``) holding
       a ``config.toml`` — dev / editable installs, where the package lives
       under the repo;
    3. the process cwd (an installed copy launched from the repo root, as
       ``mindflock serve`` is).
    """
    env = (os.environ.get("MINDFLOCK_REPO_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for parent in Path(anchor).resolve().parents:
        if (parent / "config.toml").is_file():
            return parent
    return Path.cwd()


def load_config(repo_root: Path):
    """The pipeline's layered config (env → settings.json → config.toml), with
    a relative ``workspace_dir`` re-anchored at ``repo_root`` so a differing
    server cwd can't scatter workspaces. Raises ``ConfigError`` when ingestion
    has never been configured — callers surface the message verbatim."""
    from backend.ticket_ingestion.config import load_config as _load

    cfg = _load()
    if not Path(cfg.workspace_dir).is_absolute():
        cfg.workspace_dir = repo_root / cfg.workspace_dir
    return cfg
