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

And it carries out the two voice commands that act on a pane (the router's
``scroll`` and ``interrupt``, ``POST /api/voice/route``): :func:`scroll`
moves a session's output the way the mouse wheel would — tmux copy-mode for
an app that leaves the mouse to tmux, wheel reports for one that scrolls
itself (Claude Code, opencode…: tmux's history there is redraw debris, see
``pane_find``) — and :func:`remember_scroll` / :func:`last_scroll` keep the
last scroll per (client, session) for "more" / "keep going". Typing into a
pane a scroll left in copy-mode is safe: the scroll arms ``mux.guard_typing``
for the session, and ``mux.run`` leaves copy-mode before any keys land.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from backend.session import mux

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


# ---- voice scroll ------------------------------------------------------------
#: The tmux invocation; tests point it at a private server (``-L``).
TMUX = ["tmux"]
#: A repeat ("more", "keep going") repeats a scroll this recent, else it is
#: words for the session.
SCROLL_AGAIN_S = 20.0
#: Ceiling on one command's count ("scroll up 500 lines").
SCROLL_MAX_COUNT = 50
#: Wheel notches per line, for an app that scrolls itself (xterm's three
#: lines a notch, which Claude Code and opencode follow).
WHEEL_LINES = 3
#: Notches that reach either end of an app's scrollback from anywhere
#: (Claude Code animates a burst, so one long burst, not many).
WHEEL_TO_END = 400
#: copy-mode commands for each amount (up, down).
_COPY = {
    "page": ("page-up", "page-down"),
    "half": ("halfpage-up", "halfpage-down"),
    "line": ("scroll-up", "scroll-down"),
}
#: (client, title) -> (direction, amount, count, monotonic time)
_SCROLLS: "OrderedDict[Tuple[str, str], Tuple[str, str, int, float]]" = OrderedDict()
_SCROLLS_MAX = 256


def _tmux(*args: str):
    return mux.run(
        [*TMUX, *args], capture_output=True, text=True, errors="replace", timeout=10
    )


def _pane(target: str) -> Optional[dict]:
    """The pane's geometry and modes, or None when there is no such pane."""
    out = _tmux(
        "display-message",
        "-p",
        "-t",
        target,
        "#{session_name}\t#{pane_height}\t#{pane_width}\t#{pane_in_mode}"
        "\t#{mouse_any_flag}\t#{mouse_sgr_flag}",
    )
    try:
        sess, h, w, mode, grabbed, sgr = out.stdout.rstrip("\n").split("\t")
        if out.returncode != 0 or not sess:
            return None
        return {
            "h": int(h or 0),
            "w": int(w or 0),
            "in_mode": mode == "1",
            "grabbed": grabbed == "1",
            "sgr": sgr == "1",
        }
    except ValueError:
        return None


def _notches(amount: str, count: int, h: int) -> int:
    if amount in ("top", "bottom"):
        return WHEEL_TO_END
    lines = {"page": max(1, h - 2), "half": max(1, h // 2), "line": 1}[amount]
    return max(1, (lines * count + WHEEL_LINES - 1) // WHEEL_LINES)


def _wheel(target: str, pane: dict, up: bool, n: int) -> bool:
    """``n`` wheel notches at the middle of the pane, as the terminal would
    report them (SGR when the app asked for it)."""
    x, y = max(1, pane["w"] // 2), max(1, pane["h"] // 2)
    btn = 64 if up else 65
    if pane["sgr"]:
        seq = "\x1b[<%d;%d;%dM" % (btn, x, y)
    else:
        seq = "\x1b[M" + chr(32 + btn) + chr(32 + min(x, 223)) + chr(32 + min(y, 223))
    return _tmux("send-keys", "-t", target, "-l", "--", seq * n).returncode == 0


def scroll(target: str, direction: str, amount: str, count: int = 1) -> bool:
    """Scroll tmux pane ``target`` (an agent session's ``=name:``):
    ``direction`` "up" / "down", ``amount`` "page" / "half" / "line" (×
    ``count``), "top" (the oldest line) or "bottom" (the live screen). The
    scrollback the mouse wheel moves (the rule ``pane_find.find_mode``
    keeps): an app that grabbed the mouse gets wheel reports; otherwise
    tmux copy-mode, entered with ``-e`` so scrolling back down past the
    bottom leaves it, and "bottom" leaves it. False when the pane is gone or
    tmux refused."""
    count = max(1, min(SCROLL_MAX_COUNT, int(count or 1)))
    up = direction == "up"
    pane = _pane(target)
    if pane is None:
        return False
    if pane["grabbed"] and not pane["in_mode"]:
        return _wheel(target, pane, up, _notches(amount, count, pane["h"]))
    if amount == "bottom" or (not up and not pane["in_mode"]):
        # Already at the live screen, or asked to go back to it.
        if pane["in_mode"]:
            return _tmux("send-keys", "-X", "-t", target, "cancel").returncode == 0
        return True
    if not pane["in_mode"]:
        # From here on, typing into it leaves copy-mode first (mux.run).
        mux.guard_typing(target)
        if _tmux("copy-mode", "-e", "-t", target).returncode != 0:
            return False
    if amount == "top":
        return _tmux("send-keys", "-X", "-t", target, "history-top").returncode == 0
    cmd = _COPY.get(amount, _COPY["page"])[0 if up else 1]
    return _tmux("send-keys", "-X", "-N", str(count), "-t", target, cmd).returncode == 0


def remember_scroll(
    client: str, title: str, direction: str, amount: str, count: int
) -> None:
    """``client`` just scrolled ``title`` so: what "more" repeats."""
    key = (str(client), str(title))
    with _LOCK:
        _SCROLLS.pop(key, None)
        while len(_SCROLLS) >= _SCROLLS_MAX:
            _SCROLLS.popitem(last=False)
        _SCROLLS[key] = (direction, amount, int(count), _now())


def last_scroll(client: str, title: str) -> Optional[Dict]:
    """``{dir, amount, count}`` of ``client``'s scroll of ``title`` in the
    last :data:`SCROLL_AGAIN_S`, else None."""
    with _LOCK:
        got = _SCROLLS.get((str(client), str(title)))
    if got is None or _now() - got[3] > SCROLL_AGAIN_S:
        return None
    return {"dir": got[0], "amount": got[1], "count": got[2]}


def _reset_for_tests() -> None:
    with _LOCK:
        _LIVE.clear()
        _ANSWERED_AT.clear()
        _SCROLLS.clear()
