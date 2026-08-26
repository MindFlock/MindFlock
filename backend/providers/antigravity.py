"""The Google Antigravity CLI (``agy``) provider.

Config-driven like the other bundled CLIs (launch/resume/trust/classification
all come from its :class:`ProviderConfig` in ``config.py``); this subclass adds
per-window resume-thread discovery from Antigravity's on-disk conversation
store, so several windows on one directory each resume their OWN conversation
(``agy --conversation <id>``) instead of all landing on ``--continue``'s
newest one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .generic import GenericProvider


def _conversations_dir() -> Path:
    """Antigravity's conversation store: one ``<uuid>.db`` per conversation.

    Lives under the CLI's state dir (historically ``~/.gemini/antigravity-cli``
    — agy replaced Gemini CLI and kept the location)."""
    env = os.environ.get("ANTIGRAVITY_CLI_DIR")
    base = (
        Path(env)
        if env
        else Path(os.path.expanduser("~")) / ".gemini" / "antigravity-cli"
    )
    return base / "conversations"


def find_thread_id(since_ts: Optional[float], exclude=frozenset()) -> str:
    """The id (filename stem) of the newest conversation db created/updated at
    or after ``since_ts``, skipping ids other windows claimed. ``""`` when
    nothing matches. Never raises.

    Antigravity's store is global (not per-cwd), so the claimed-set exclusion
    plus the launch-time bound is what keeps sibling windows from binding to
    each other's conversations."""
    try:
        best, best_ts = "", -1.0
        for f in _conversations_dir().glob("*.db"):
            try:
                ts = f.stat().st_mtime
            except OSError:
                continue
            # 5s slack: the db can be stamped moments before tmux reports the
            # pane's creation time.
            if since_ts is not None and ts < since_ts - 5.0:
                continue
            if f.stem in exclude:
                continue
            if ts > best_ts:
                best, best_ts = f.stem, ts
        return best
    except Exception:  # noqa: BLE001 — discovery is enrichment only
        return ""


class AntigravityProvider(GenericProvider):
    # --- live plan quota ---------------------------------------------------- #
    def usage_live(self) -> Optional[dict]:
        """Per-group weekly quota from a running agy's local language server."""
        try:
            from . import antigravity_usage_api

            return antigravity_usage_api.live_usage()
        except Exception:  # noqa: BLE001 — live usage is enrichment only
            return None

    # --- per-session resume thread ----------------------------------------- #
    def record_thread(
        self,
        session_name: str,
        workdir: str,
        since_ts=None,
        profile_id: str = "",
    ) -> None:
        try:
            from . import thread_markers

            tid = find_thread_id(
                since_ts, exclude=thread_markers.claimed(exclude_session=session_name)
            )
            if tid and tid != thread_markers.read(session_name):
                thread_markers.record(session_name, tid, profile_id)
        except Exception:  # noqa: BLE001 — thread binding is enrichment only
            pass
