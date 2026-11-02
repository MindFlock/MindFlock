"""Single-use capability tokens behind the Yes / No buttons of a dialog push.

A ``needs_input`` push can carry ntfy ``http`` action buttons, so a locked
phone answers a permission dialog with one tap. The button's URL is stored
on the ntfy server and sits on a lock screen, so it must carry **no
standing credential**: not the access token, not anything that answers a
second dialog. Each button is therefore a token minted here that stands for
exactly one answer — ``(session title, dialog id, key)`` — and is good once,
for ten minutes.

:func:`issue` mints one; the notify addon's ``POST /api/notify/act/<token>``
calls :func:`consume` and then does what ``POST /api/instances/<title>/answer``
would have done with that key and ``dialog_id``, so the dialog-id pin, the
per-session answer lock and the "just answered" guard all still apply. A
token that was used, expired, or never issued consumes to ``None`` (404).

Consuming one token also drops its siblings — the other buttons minted for
the same dialog — so after a Yes the No cannot land on an *identical* prompt
that comes up later with the same id. In memory only: a restart forgets every
outstanding button, which is the right failure (a button older than the
server is answered by nobody). At most :data:`MAX_LIVE` live at once, oldest
evicted.

It also keeps WHEN each session's dialog last got an answer
(:func:`answered_at`, stamped by :func:`forget` with a ``dialog_id`` — the
call ``/answer`` makes after every settling answer, by voice, by hand, from
the pop-up or from a push button). The voice router reads it: a phrase begun
before the previous answer can never answer the next dialog, even one that
re-asks with the same id (``voice_router.dialog_visible_at``).
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from typing import Dict, Optional

#: How long a button stays answerable. Longer than the dialog can usefully
#: wait is pointless: the ``dialog_id`` pin refuses an answer to a prompt that
#: is no longer on screen anyway.
TTL_S = 600.0
#: Ceiling on outstanding buttons (two or three per dialog push); the oldest
#: go first.
MAX_LIVE = 200

_LOCK = threading.Lock()
#: token -> {title, dialog_id, key, exp}; insertion order = issue order.
_LIVE: "OrderedDict[str, Dict]" = OrderedDict()

_now = time.monotonic  # patched by tests to age a token

#: title -> epoch (``time.time()``, the clock the voice router reads phrase
#: timing on) of the last settling answer to one of its dialogs.
_ANSWERED_AT: Dict[str, float] = {}
_ANSWERED_MAX = 1024
_clock = time.time  # patched by tests


def _sweep(now: float) -> None:
    for tok in [t for t, e in _LIVE.items() if e["exp"] <= now]:
        _LIVE.pop(tok, None)


def issue(title: str, dialog_id: str, key: str) -> str:
    """Mint a token that answers ``dialog_id`` on ``title`` with ``key``.

    32 lower-case hex characters (``secrets.token_hex(16)``) — the auth gate's
    public-route regex matches exactly that shape and nothing else."""
    tok = secrets.token_hex(16)
    now = _now()
    with _LOCK:
        _sweep(now)
        while len(_LIVE) >= MAX_LIVE:
            _LIVE.popitem(last=False)
        _LIVE[tok] = {
            "title": str(title),
            "dialog_id": str(dialog_id),
            "key": str(key),
            "exp": now + TTL_S,
        }
    return tok


def consume(token: str) -> Optional[Dict[str, str]]:
    """``{title, dialog_id, key}`` for a live token — once. ``None`` for a
    token that is unknown, already used or expired."""
    if not isinstance(token, str) or not token:
        return None
    with _LOCK:
        entry = _LIVE.pop(token, None)
        if entry is None:
            return None
        if entry["exp"] <= _now():
            return None
        # The other buttons of the same dialog are spent with this one.
        for tok, other in list(_LIVE.items()):
            if (
                other["title"] == entry["title"]
                and other["dialog_id"] == entry["dialog_id"]
            ):
                _LIVE.pop(tok, None)
    return {
        "title": entry["title"],
        "dialog_id": entry["dialog_id"],
        "key": entry["key"],
    }


def forget(title: str, dialog_id: Optional[str] = None) -> int:
    """Drop every outstanding button for ``title`` — only those of
    ``dialog_id`` when given. Called when the dialog was answered some other
    way (the desktop, the phone page) and when the session is removed, so a
    button on the lock screen never answers a LATER prompt that happens to
    hash to the same id (an agent re-asking the same ``git push``). Returns
    how many were dropped.

    With a ``dialog_id`` (``/answer``'s call after a settling answer) it
    also stamps :func:`answered_at`; without one (session removal) it drops
    the stamp."""
    if dialog_id is not None:
        note_answered(title, dialog_id)
    with _LOCK:
        if dialog_id is None:
            _ANSWERED_AT.pop(str(title), None)
        gone = [
            tok
            for tok, e in _LIVE.items()
            if e["title"] == str(title)
            and (dialog_id is None or e["dialog_id"] == str(dialog_id))
        ]
        for tok in gone:
            _LIVE.pop(tok, None)
    return len(gone)


def _ticks(dialog_id) -> bool:
    """Whether ``dialog_id``'s digits TOGGLE rather than answer (a
    multi-select question, as ``agent_io.current_dialog`` saw it): a tick
    leaves that same dialog up, and the next tick is welcome."""
    try:
        from backend.web.core import agent_io as _agent_io

        return str(dialog_id) in getattr(_agent_io, "_TOGGLE_IDS", {})
    except Exception:  # noqa: BLE001 — unknown reads as an answer (stricter)
        return False


def note_answered(title: str, dialog_id: Optional[str] = None) -> None:
    """``title``'s dialog ``dialog_id`` just got a settling answer: stamp
    :func:`answered_at` (a multi-select tick is not an answer)."""
    if dialog_id is not None and _ticks(dialog_id):
        return
    now = _clock()
    with _LOCK:
        if len(_ANSWERED_AT) >= _ANSWERED_MAX and str(title) not in _ANSWERED_AT:
            _ANSWERED_AT.clear()
        _ANSWERED_AT[str(title)] = now


def answered_at(title: str) -> float:
    """Epoch of the last settling answer to one of ``title``'s dialogs, any
    path — 0.0 when none since this server started (or the session was
    removed). Never raises."""
    with _LOCK:
        return _ANSWERED_AT.get(str(title), 0.0)


def live_count() -> int:
    """How many buttons are outstanding (expired ones included until swept)."""
    with _LOCK:
        return len(_LIVE)


def _reset_for_tests() -> None:
    with _LOCK:
        _LIVE.clear()
        _ANSWERED_AT.clear()
