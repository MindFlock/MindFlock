"""The per-session voice flag: "this session is being talked to from a phone".

While the flag is on, every prompt the session receives carries the voice
instruction (:data:`backend.web.core.spoken.INSTRUCTION`), so the agent ends
each reply with a ``Say:`` line the phone can speak. The flag is a FILE on
purpose: the CLI's own prompt-submit hook
(:func:`backend.providers.activity_markers.hook_command`) must see it at fire
time, from inside the agent's process, with no server round trip —
``<flag dir>/<tmux session>.on`` exists or it does not, and the hook reads
nothing else. Same directory conventions as the activity markers:
``MINDFLOCK_VOICE_FLAG_DIR`` overrides ``~/.mindflock-assistant/.voice``,
through the home guard so a test can never flip a real session's flag.

Keyed by the TMUX SESSION NAME the hook resolves (``tmux.to_mindflock_tmux_
name(title)``), with :func:`activity_markers.safe_session_name`'s
sanitisation — never by title. Every reader here is cheap (one ``stat``) and
never raises: a flag that cannot be read is a flag that is off.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.providers import activity_markers as _am


def flag_dir() -> Path:
    """The directory voice flags live in (``MINDFLOCK_VOICE_FLAG_DIR``, else
    ``~/.mindflock-assistant/.voice``), guarded like the marker dir."""
    from backend.config.home_guard import guard

    return Path(
        guard(
            os.environ.get(_am.VOICE_FLAG_DIR_ENV)
            or os.path.join(os.path.expanduser("~"), *_am.VOICE_FLAG_SUBDIR),
            "voice flag dir",
        )
    )


def flag_path(session_name: str) -> Path:
    """``<flag dir>/<session>.on`` — the file the hook tests for."""
    return flag_dir() / (_am.safe_session_name(session_name) + ".on")


def is_on(session_name: str) -> bool:
    """Whether voice mode is on for the tmux session ``session_name``."""
    if not session_name:
        return False
    try:
        return flag_path(session_name).is_file()
    except Exception:  # noqa: BLE001 — unreadable (or guarded) reads as off
        return False


def set_on(session_name: str, on: bool) -> bool:
    """Turn voice mode on or off for ``session_name``; returns the new state.

    Idempotent: turning on an on session rewrites the (one-byte) flag, turning
    off an off session is a no-op. An ``OSError`` propagates so the route can
    say why (the state it reports is read back, never assumed)."""
    path = flag_path(session_name)
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n", encoding="utf-8")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return is_on(session_name)


def forget(session_name: str) -> None:
    """Drop ``session_name``'s flag (session removal): a closed session leaves
    no flag behind, and a namesake does not inherit voice mode. Never raises."""
    try:
        set_on(session_name, False)
    except Exception:  # noqa: BLE001 — a removal must not fail over cleanup
        pass
