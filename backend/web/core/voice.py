"""The per-session voice flags: "a phone is talking to this session" and
"speak THIS reply".

Two files, both keyed the same way:

* ``<flag dir>/<tmux session>.on`` — the sticky marker, set by ``POST
  /api/instances/{title}/voice {on}``: a phone is in a voice conversation
  with this session. It drives the row's ``voice`` field and the voice
  route's hook re-pin; it NO LONGER injects anything into prompts (the Stop
  hook files every reply whether or not it is set).
* ``<flag dir>/<tmux session>.next`` — the ONE-SHOT request, armed by the
  ``/send`` delivery right before a ``say: true`` prompt's keystrokes go to a
  CLI whose prompt hook injects the instruction (Claude): the file holds the
  arming unix time, and the CLI's own prompt-submit hook
  (:func:`backend.providers.activity_markers.hook_command`) prints the voice
  instruction (:data:`backend.web.core.spoken.INSTRUCTION`) when it is
  younger than :data:`NEXT_MAX_AGE_S` and deletes it either way — so only
  that one reply ends with a ``Say:`` line the phone can speak, and a prompt
  that never got submitted cannot lend its line to the next one.

They are FILES on purpose: the hook must see them at fire time, from inside
the agent's process, with no server round trip. Same directory conventions
as the activity markers: ``MINDFLOCK_VOICE_FLAG_DIR`` overrides
``~/.mindflock-assistant/.voice``, through the home guard so a test can never
flip a real session's flag.

Keyed by the TMUX SESSION NAME the hook resolves (``tmux.to_mindflock_tmux_
name(title)``), with :func:`activity_markers.safe_session_name`'s
sanitisation — never by title. Every reader here is cheap (one ``stat``) and
never raises: a flag that cannot be read is a flag that is off.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from backend.providers import activity_markers as _am

#: How long an armed ``.next`` stays good (seconds) — the hook's own cutoff
#: (:data:`activity_markers.VOICE_NEXT_MAX_AGE_S`).
NEXT_MAX_AGE_S = _am.VOICE_NEXT_MAX_AGE_S


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
    """``<flag dir>/<session>.on`` — the sticky conversation marker."""
    return flag_dir() / (_am.safe_session_name(session_name) + ".on")


def next_path(session_name: str) -> Path:
    """``<flag dir>/<session>.next`` — the one-shot file the hook consumes."""
    return flag_dir() / (_am.safe_session_name(session_name) + ".next")


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


def arm_next(session_name: str) -> None:
    """Ask ``session_name``'s next submitted prompt to end its reply with the
    ``Say:`` line: write ``.next`` holding the arming time. The hook honours
    it for :data:`NEXT_MAX_AGE_S` and deletes it on the next prompt either
    way. An ``OSError`` propagates (like :func:`set_on`) so the caller can
    fall back; a blank name is a no-op."""
    if not session_name:
        return
    path = next_path(session_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("%d\n" % int(time.time()), encoding="utf-8")


def disarm_next(session_name: str) -> None:
    """Drop ``session_name``'s one-shot request (a delivery that failed, a
    removed session). Never raises."""
    if not session_name:
        return
    try:
        next_path(session_name).unlink()
    except Exception:  # noqa: BLE001 — absent or unreadable: nothing armed
        pass


def say_all_path() -> Path:
    """``<flag dir>/say-all`` — the standing request (every prompt, every
    session) the hook honours like a young ``.next``."""
    return flag_dir() / _am.VOICE_SAY_ALL_FILE


def say_all() -> bool:
    """Whether every prompt is being asked for its ``Say:`` line."""
    try:
        return say_all_path().is_file()
    except Exception:  # noqa: BLE001 — unreadable reads as off
        return False


def set_say_all(on: bool) -> bool:
    """Ask every prompt in every session for the ``Say:`` line (a listener
    has "read everything" on), or stop. Idempotent; an ``OSError``
    propagates so the route can say why; the state reported is read back."""
    path = say_all_path()
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("%d\n" % int(time.time()), encoding="utf-8")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return say_all()


def forget(session_name: str) -> None:
    """Drop ``session_name``'s flags (session removal): a closed session
    leaves no flag behind, and a namesake does not inherit voice mode. Never
    raises."""
    try:
        set_on(session_name, False)
    except Exception:  # noqa: BLE001 — a removal must not fail over cleanup
        pass
    disarm_next(session_name)
