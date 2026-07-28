"""Prune stale workspaces on startup.

On boot the pipeline removes any top-level entry under ``workspace_dir`` that
has been untouched for ``max_age_seconds``. Two guards keep a workspace an
agent is still working in from being deleted:

* **Live-session guard (primary):** any workspace containing the current
  working directory of a live tmux pane is skipped — a long-running detached
  session keeps its workspace regardless of age.
* **Recency from the tree, not the top-level dir:** a nested edit doesn't bump
  the top-level dir's mtime, so age is computed from the newest mtime found in
  a bounded walk of the tree (birth time is unavailable on most Linux
  filesystems, including the WSL2/ext4 host this runs on).
"""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from backend.workspace_setup import is_refresher_dirname

_logger = logging.getLogger(__name__)

_MAX_WORKSPACE_AGE_SECONDS = 3 * 24 * 60 * 60  # 3 days
# Cap on how many entries the recency walk stats per workspace. Recent files
# usually surface early; the cap keeps startup fast on huge checkouts.
_RECENCY_WALK_MAX_ENTRIES = 512

# Long-lived workspaces that must survive cleanup regardless of age. Cache
# refreshers (e.g. `_testmon_refresher`) reuse their workspace across runs to
# keep setup (`uv sync`) and the cache artifact incremental; deleting one would
# force a full re-clone and a cold rebuild of the cache.
_PRESERVED_NAMES: frozenset[str] = frozenset()


def _live_session_paths() -> set[str]:
    """Current working directory of every pane in every live tmux session.

    Story/PR sessions run inside their workspace (tmux ``-c <dir>``), so a
    workspace containing any of these paths is actively in use. Best-effort:
    a missing/unresponsive tmux yields an empty set (age rules still apply).
    """
    try:
        proc = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_current_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _is_in_use(entry: Path, live_paths: set[str]) -> bool:
    """True when a live tmux pane's cwd is ``entry`` or inside it."""
    if not live_paths:
        return False
    root = str(entry.resolve())
    prefix = root + os.sep
    return any(p == root or p.startswith(prefix) for p in live_paths)


def _newest_mtime(path: Path, max_entries: int = _RECENCY_WALK_MAX_ENTRIES) -> float:
    """Newest mtime in ``path``'s tree (bounded walk; includes ``path`` itself).

    The top-level dir's own mtime doesn't change for nested edits, so a
    long-running session's workspace would otherwise look untouched-for-days
    while an agent is still writing deep inside it.
    """
    st = path.stat()
    # Use the most recent of mtime and birthtime. On platforms exposing
    # st_birthtime (macOS), a fresh workspace whose files carry old mtimes (e.g.
    # a checkout) is still protected; but birthtime alone must never mask a newer
    # mtime — and it doesn't reflect an mtime set via os.utime, which the tests
    # rely on. max() keeps both correct.
    newest = max(st.st_mtime, getattr(st, "st_birthtime", 0.0) or 0.0)
    if not path.is_dir() or path.is_symlink():
        return newest
    seen = 0
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            if seen >= max_entries:
                return newest
            seen += 1
            try:
                mtime = os.lstat(os.path.join(root, name)).st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
    return newest


def prune_stale_workspaces(
    workspace_dir: Path,
    max_age_seconds: float = _MAX_WORKSPACE_AGE_SECONDS,
    now: float | None = None,
    preserve: frozenset[str] = _PRESERVED_NAMES,
) -> int:
    """Delete entries directly under ``workspace_dir`` older than ``max_age_seconds``.

    Entries whose name is in ``preserve`` are always kept (e.g. the long-lived
    testmon refresher workspace), as is any workspace with a live tmux session
    working inside it. Returns the number of entries removed. Never raises: a
    missing directory is a no-op, and individual stat/remove failures are
    logged and skipped so a cleanup problem can't block startup.
    """
    workspace_dir = Path(workspace_dir)
    if not workspace_dir.is_dir():
        return 0

    now_ts = time.time() if now is None else now
    cutoff = now_ts - max_age_seconds
    removed = 0
    live_paths = _live_session_paths()

    for entry in workspace_dir.iterdir():
        if entry.name in preserve or is_refresher_dirname(entry.name):
            continue
        if _is_in_use(entry, live_paths):
            _logger.info(
                "Keeping workspace %s: a live tmux session is working in it",
                entry,
            )
            continue
        try:
            touched = _newest_mtime(entry)
        except OSError as e:
            _logger.warning("Could not stat %s during cleanup: %s", entry, e)
            continue
        if touched >= cutoff:
            continue

        age_days = (now_ts - touched) / 86400
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as e:
            _logger.warning("Failed to remove stale workspace %s: %s", entry, e)
            continue

        removed += 1
        _logger.info("Removed stale workspace %s (age %.1f days)", entry, age_days)

    if removed:
        _logger.info(
            "Workspace cleanup removed %d stale entr%s from %s",
            removed,
            "y" if removed == 1 else "ies",
            workspace_dir,
        )
    return removed
