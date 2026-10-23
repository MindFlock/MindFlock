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
    how many were dropped."""
    with _LOCK:
        gone = [
            tok
            for tok, e in _LIVE.items()
            if e["title"] == str(title)
            and (dialog_id is None or e["dialog_id"] == str(dialog_id))
        ]
        for tok in gone:
            _LIVE.pop(tok, None)
    return len(gone)


def live_count() -> int:
    """How many buttons are outstanding (expired ones included until swept)."""
    with _LOCK:
        return len(_LIVE)


def _reset_for_tests() -> None:
    with _LOCK:
        _LIVE.clear()
