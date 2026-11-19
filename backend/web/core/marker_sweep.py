"""Prune the activity-marker directory of files no session can still need.

``~/.mindflock-assistant/.activity-markers`` (``activity_markers.marker_dir``)
gets one file per tmux session a hooked agent CLI ever ran in, and nothing
removed them: 1,673 files on the owner's box on 2026-11-19, 1,466 of them more
than a week old. The reply watch rescans the directory every time any hook
writes a marker (``state_events.ReplyWatch``), so the dead files cost
something on every turn of every session.

**What lives there, and who reads it** (established from the code, not
assumed):

* ``<session>.json`` — ``{"state", "ts"}``, written by the activity hooks
  (``activity_markers.hook_command`` / ``notification_hook_command``) inside
  ANY tmux session whose CLI has the hooks: MindFlock's own sessions, the
  ticket pipeline's, a second server's, a Cursor window's. Every reader goes
  through ``activity_markers._read_entry``, which ignores a marker whose
  ``ts`` is older than 6 hours (``_ACTIVITY_STALE_AFTER``); the dialog watch
  only stats it for a live row.
* ``<session>.reply.json`` — the Stop hook's ``{"ts", "text"}`` (written
  through ``<session>.reply.json.<pid>.tmp`` and ``os.replace``). Read by
  ``spoken.read_reply`` with NO age limit: a session idle for a week still
  answers "what did it last say" and the question detection from it. So a
  reply of a session that still exists must stay, however old.
  ``spoken.forget`` already drops it when a session is removed through a
  route; a session that left another way kept it.
* Thread markers are NOT here (``thread_markers.marker_dir`` is
  ``.thread-markers``) and are never touched.

**Provably dead** (:func:`sweep`): a file is removed only when ALL hold —

1. its session is not live: not a tmux session on this machine's tmux
   server (any session, not only MindFlock's) and not a session in this
   server's state (which both servers and the pipeline share through
   ``state.json``, so a sleeping or offline session keeps its files);
2. it is older (mtime) than :data:`MAX_AGE_S` (7 days) — a session that
   ended this week, or one the state has not adopted yet, keeps its files;
3. it has one of the kinds above (anything else is left alone).

If tmux cannot be asked the pass removes nothing: "not live" cannot be
proved. A leftover ``.tmp`` older than the same age goes too (its writer is
long gone). At most :data:`MAX_PER_PASS` files go per pass, so a backlog
drains over a few passes instead of one burst of I/O.

**When**: :func:`run_loop`, on the ticks' own thread pool, never the event
loop: once :data:`FIRST_DELAY_S` after boot (past the boot quiet window), then
every :data:`INTERVAL_S`. Off under pytest (``ENGINE`` is the developer's
real state there) and with ``MINDFLOCK_MARKER_SWEEP=0``. Never raises.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Callable, Iterable, Optional, Set

#: A dead session's files are kept this long after their last write.
MAX_AGE_S = 7 * 24 * 3600.0
#: Files removed per pass at most.
MAX_PER_PASS = 500
#: The first pass, after boot.
FIRST_DELAY_S = 120.0
#: Between passes.
INTERVAL_S = 6 * 3600.0

#: Test seam: None = on except under pytest / MINDFLOCK_MARKER_SWEEP=0.
ENABLED: Optional[bool] = None

_REPLY = ".reply.json"
_STATE = ".json"


def enabled() -> bool:
    if ENABLED is not None:
        return bool(ENABLED)
    if os.environ.get("MINDFLOCK_MARKER_SWEEP", "1") == "0":
        return False
    from backend.web.core import restart as _restart

    return not _restart._under_pytest()


def _session_of(name: str) -> Optional[str]:
    """The (safe) session name a marker-directory file belongs to, or None
    for a file this sweep does not own."""
    if name.endswith(_REPLY):
        return name[: -len(_REPLY)]
    if name.endswith(".tmp") and _REPLY + "." in name:
        return name[: name.index(_REPLY + ".")]
    if name.endswith(_STATE):
        return name[: -len(_STATE)]
    return None


def sweep(
    directory,
    live: Set[str],
    *,
    now: Optional[float] = None,
    max_age_s: float = MAX_AGE_S,
    limit: int = MAX_PER_PASS,
    unlink: Callable[[str], None] = os.unlink,
) -> int:
    """Remove the dead, old files from ``directory`` (see the module
    docstring); ``live`` = the safe session names still needed. Returns how
    many were removed. A file that cannot be stat'ed or removed is skipped.
    Never raises."""
    now = time.time() if now is None else now
    removed = 0
    try:
        it = os.scandir(str(directory))
    except OSError:
        return 0
    with it:
        for entry in it:
            if removed >= limit:
                break
            try:
                session = _session_of(entry.name)
                if not session or session in live:
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if now - entry.stat(follow_symlinks=False).st_mtime < max_age_s:
                    continue
                unlink(entry.path)
                removed += 1
            except Exception:  # noqa: BLE001 — one file never stops the pass
                continue
    return removed


def _tmux_sessions() -> Optional[Set[str]]:
    """Every tmux session name on this machine's server, ``set()`` with no
    server running, None when tmux cannot be asked."""
    try:
        from backend.session import mux

        out = mux.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        # tmux says why on stderr; only "no server" means "no sessions".
        err = (out.stderr or "").lower()
        if "no server" in err or "error connecting" in err:
            return set()
        return None
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def live_names(titles: Iterable[str], tmux_names: Iterable[str]) -> Set[str]:
    """The safe (file) names of every session still needed: this server's
    sessions (by their tmux name) and every live tmux session."""
    from backend.providers import activity_markers as am
    from backend.session import tmux

    out: Set[str] = set()
    for t in titles:
        try:
            out.add(am.safe_session_name(tmux.to_mindflock_tmux_name(t)))
        except Exception:  # noqa: BLE001
            continue
    for n in tmux_names:
        out.add(am.safe_session_name(n))
    return out


def pass_once(srv) -> int:
    """One sweep of the real marker directory; 0 when liveness cannot be
    established. Never raises."""
    try:
        names = _tmux_sessions()
        if names is None:
            return 0
        titles = list(getattr(srv, "ENGINE").instances.keys())
        from backend.providers import activity_markers as am

        n = sweep(am.marker_dir(), live_names(titles, names))
        if n:
            log = getattr(srv, "log", None)
            logger = getattr(log, "InfoLog", None) or getattr(log, "ErrorLog", None)
            if logger is not None:
                logger.Printf("activity markers: removed %d dead session files", n)
        return n
    except Exception:  # noqa: BLE001
        return 0


async def run_loop(srv, first_delay: float = FIRST_DELAY_S) -> None:
    """:func:`pass_once` after ``first_delay`` and then every
    :data:`INTERVAL_S`, on the ticks' own pool (``srv._in_bg_pool``)."""
    await asyncio.sleep(first_delay)
    while True:
        try:
            await srv._in_bg_pool(pass_once, srv)
        except Exception:  # noqa: BLE001 — the sweep must never die
            pass
        await asyncio.sleep(INTERVAL_S)
