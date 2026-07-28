"""Per-session resume-thread markers.

Several sessions can share one working directory (in-place sessions and window
copies on the same repo). The CLIs' bulk resume flags (``claude --continue``,
``codex resume --last``) pick the NEWEST conversation for that directory, so
after a restart every sibling resumed the same thread. These markers record
which conversation/session id belongs to which MindFlock window (keyed by its
tmux session name), so each window can resume ITS OWN thread by id.

Writers: the Claude Code activity hooks (which receive ``session_id`` on
stdin) and the pollers' ``provider.record_thread()``. Readers: the providers'
resume launch-command builders. Mirrors the exit-/activity-marker layout.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Set


def marker_dir() -> Path:
    """Directory holding the per-session ``<session>.thread`` marker files.

    Defaults to ``~/.mindflock-assistant/.thread-markers``;
    ``MINDFLOCK_THREAD_MARKER_DIR`` overrides it (tests point it at a tmp dir).
    """
    return Path(
        os.environ.get(
            "MINDFLOCK_THREAD_MARKER_DIR",
            os.path.join(
                os.path.expanduser("~"), ".mindflock-assistant", ".thread-markers"
            ),
        )
    )


def _path(session_name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_name or "")
    return marker_dir() / (safe + ".thread")


def record(session_name: str, thread_id: str) -> None:
    """Persist ``thread_id`` for ``session_name`` (best-effort, never raises)."""
    if not session_name or not thread_id:
        return
    try:
        d = marker_dir()
        d.mkdir(parents=True, exist_ok=True)
        _path(session_name).write_text(thread_id.strip(), encoding="utf-8")
    except Exception:  # noqa: BLE001 — markers are enrichment only
        pass


def _valid(tid: str) -> bool:
    # A thread id is a short single-line token (uuid-ish); anything else is a
    # garbled marker and must not be spliced into a shell command.
    return bool(tid) and re.fullmatch(r"[A-Za-z0-9._:-]{4,128}", tid) is not None


def read(session_name: str) -> str:
    """The recorded thread id for ``session_name``, or ``""``. Never raises."""
    try:
        tid = _path(session_name).read_text(encoding="utf-8").strip()
        if _valid(tid):
            return tid
    except Exception:  # noqa: BLE001
        pass
    return ""


def clear(session_name: str) -> None:
    """Drop the marker (fresh launches must not resume a stale thread)."""
    try:
        _path(session_name).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def claimed(exclude_session: Optional[str] = None) -> Set[str]:
    """Every thread id currently claimed by OTHER sessions' markers.

    Used by pollers' discovery so a window never binds to a sibling's
    conversation. Never raises.
    """
    out: Set[str] = set()
    try:
        skip = _path(exclude_session).name if exclude_session else None
        for f in marker_dir().glob("*.thread"):
            if skip and f.name == skip:
                continue
            try:
                tid = f.read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                continue
            if _valid(tid):
                out.add(tid)
    except Exception:  # noqa: BLE001
        pass
    return out
