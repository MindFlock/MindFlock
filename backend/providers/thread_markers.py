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

**Two kinds of file, on purpose.**

``<session>.thread`` is the CURRENT thread — the one every reader wants, and
the only one that existed before auth profiles. ``<session>@<profile>.thread``
is a per-ACCOUNT memory of the last thread that window had while running as
that identity.

A conversation belongs to the account that created it: transcripts live under
that account's config dir, and the other identity cannot resume them. Without
the memory files a swap was doubly lossy — the new identity failed to resume a
thread it had never seen (fine, it starts fresh) and then *overwrote* the one
id that could have taken you back, so swapping back also started fresh. The
memory is written alongside every record and replayed by :func:`switch_profile`
on a swap, so the CURRENT marker always names a thread the identity about to
run can actually open. Readers stay pointed at ``<session>.thread`` and never
learn about accounts.

``@`` cannot occur in a sanitized session name (the sanitizer keeps only
``[A-Za-z0-9_.-]``), so it is unambiguous as the separator.
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


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name or "")


def _path(session_name: str) -> Path:
    """The CURRENT-thread marker — what every reader resolves."""
    return marker_dir() / (_safe(session_name) + ".thread")


#: Memory-file name for the CLI's own ambient login. Safe as a key because a
#: profile can never BE named "default" — that string is the reserved sentinel
#: meaning "no profile" (``auth_profiles.AMBIENT_ID``).
_AMBIENT_KEY = "default"


def _account_path(session_name: str, profile_id: str) -> Path:
    """This window's remembered thread while running as ``profile_id``.

    The ambient login gets a memory too (under :data:`_AMBIENT_KEY`): its
    thread used to live only in the current marker, which a swap away
    overwrites — so without one, swapping to an account and back lost the
    conversation you started on your own login. Nothing writes an ambient
    memory until the first swap, so a user who never configures a profile
    still has exactly one file per window.
    """
    key = _safe(profile_id) or _AMBIENT_KEY
    return marker_dir() / ("%s@%s.thread" % (_safe(session_name), key))


def record(session_name: str, thread_id: str, profile_id: str = "") -> None:
    """Persist ``thread_id`` as ``session_name``'s current thread.

    With ``profile_id``, also remembers it as that account's thread for this
    window, so a later swap back can restore it. Best-effort, never raises.
    """
    if not session_name or not thread_id:
        return
    try:
        d = marker_dir()
        d.mkdir(parents=True, exist_ok=True)
        tid = thread_id.strip()
        _path(session_name).write_text(tid, encoding="utf-8")
        # Only a PROFILED record files a memory here. An ambient one would
        # otherwise create a second file for every window on every machine
        # where accounts are never used; :func:`switch_profile` files the
        # ambient memory at the only moment it can matter — the first swap.
        if profile_id:
            _account_path(session_name, profile_id).write_text(tid, encoding="utf-8")
    except Exception:  # noqa: BLE001 — markers are enrichment only
        pass


def remembered(session_name: str, profile_id: str) -> str:
    """The thread this window last had while running as ``profile_id``, or
    ``""``. Never raises."""
    try:
        tid = (
            _account_path(session_name, profile_id).read_text(encoding="utf-8").strip()
        )
        if _valid(tid):
            return tid
    except Exception:  # noqa: BLE001
        pass
    return ""


def switch_profile(session_name: str, old_profile_id: str, new_profile_id: str) -> str:
    """Point the CURRENT marker at the thread ``new_profile_id`` last had here.

    Called on an account swap, before the agent is relaunched. The outgoing
    identity's thread is filed under its own name first (the current marker may
    have moved on since it was last recorded), then the incoming identity's
    memory becomes current — or the marker is cleared when it has none, so the
    relaunch starts a fresh conversation instead of asking the new account to
    resume a thread that is not its own.

    Returns the restored thread id, or ``""`` when the new identity starts
    fresh. Never raises.
    """
    if not session_name or old_profile_id == new_profile_id:
        return read(session_name)
    try:
        current = read(session_name)
        if current:
            old_acc = _account_path(session_name, old_profile_id)
            old_acc.parent.mkdir(parents=True, exist_ok=True)
            old_acc.write_text(current, encoding="utf-8")
        restored = remembered(session_name, new_profile_id)
        if restored:
            _path(session_name).write_text(restored, encoding="utf-8")
        else:
            _path(session_name).unlink(missing_ok=True)
        return restored
    except Exception:  # noqa: BLE001
        return ""


def _valid(tid: str) -> bool:
    # A thread id is a short single-line token (uuid-ish); anything else is a
    # garbled marker and must not be spliced into a shell command.
    return bool(tid) and re.fullmatch(r"[A-Za-z0-9._:-]{4,128}", tid) is not None


def read(session_name: str) -> str:
    """The CURRENT thread id for ``session_name``, or ``""``. Never raises."""
    try:
        tid = _path(session_name).read_text(encoding="utf-8").strip()
        if _valid(tid):
            return tid
    except Exception:  # noqa: BLE001
        pass
    return ""


def clear(session_name: str, forget_accounts: bool = False) -> None:
    """Drop the current marker (fresh launches must not resume a stale thread).

    The per-account memories are kept by default: a fresh start under one
    identity says nothing about the thread another identity still owns.
    ``forget_accounts=True`` drops those too, for a window that is going away.
    """
    try:
        _path(session_name).unlink(missing_ok=True)
        if forget_accounts:
            for f in marker_dir().glob(_safe(session_name) + "@*.thread"):
                f.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def claimed(exclude_session: Optional[str] = None) -> Set[str]:
    """Every thread id currently claimed by OTHER sessions' markers.

    Used by pollers' discovery so a window never binds to a sibling's
    conversation. Never raises.
    """
    out: Set[str] = set()
    try:
        # Skip every marker belonging to the excluded window — its current one
        # AND its per-account memories. Those are threads it owns, not a
        # sibling's, and counting them would stop a window from re-binding to
        # its own conversation after a swap back.
        skip_prefix = _safe(exclude_session) if exclude_session else None
        for f in marker_dir().glob("*.thread"):
            if skip_prefix and (
                f.name == skip_prefix + ".thread"
                or f.name.startswith(skip_prefix + "@")
            ):
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
