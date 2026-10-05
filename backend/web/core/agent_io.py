"""Reading an agent's output and answering its prompts — for other agents.

The control surface an orchestrator (the MindFlock MCP) drives a worker
session through, behind two routes:

* ``GET /api/instances/{title}/output`` — what the agent produced, in one of
  three views: ``last_reply`` (the provider's newest assistant message),
  ``transcript`` (the same text ``/history?pane=agent`` serves) or ``screen``
  (the pane's VISIBLE screen only — what a dialog the agent is blocked on
  looks like). Always tail-truncated: the caller is a model with a context
  budget, and the end of a conversation is the part that answers "what
  happened".
* ``POST /api/instances/{title}/answer`` — type into a dialog the agent is
  blocked on (permission prompt, usage-limit menu) with a strict key
  allow-list and literal text, never an implicit Enter. A ``dialog_id`` pins
  the answer to the dialog it was meant for; ``by: "user"`` marks a person's
  click (the UI's answer buttons) as human presence.
* ``GET /api/instances/{title}/dialog`` — that dialog as data (question,
  command, the dialog's own numbered options), parsed by the session's
  provider (:func:`current_dialog`), for the UI's answer buttons.

The validation and tmux plumbing live here; the routes in ``server.py`` own the
registry lookups and the activity gate. tmux calls go through the server's
``_run_capped`` (read off the server namespace so tests can patch it).
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

from backend.web.core import agent_sessions as _agent_sessions

#: The output views and their default/maximum sizes (characters).
OUTPUT_VIEWS = ("last_reply", "transcript", "screen")
DEFAULT_MAX_CHARS = 6000
MAX_MAX_CHARS = 50000

#: Keys an answer may press, by tmux key name. Navigation, confirm/cancel and
#: the single-character choices a CLI dialog offers — nothing that edits text
#: (no Backspace/C-u) or signals the process (no C-c/C-d/C-z).
ANSWER_KEYS = frozenset(
    ["Enter", "Escape", "Up", "Down", "Left", "Right", "Tab", "BTab", "Space"]
    + [str(n) for n in range(1, 10)]
    + ["y", "n"]
)
MAX_ANSWER_TEXT = 2000
MAX_ANSWER_KEYS = 20
#: Who pressed an answer: an agent (the MCP's ``answer_prompt``, the default)
#: or a person clicking a dialog button in the UI.
ANSWER_BY = ("agent", "user")
#: Ceiling on a ``dialog_id`` (they are 12 hex characters).
MAX_DIALOG_ID = 64
#: Activities in which an agent is waiting on a dialog an answer can address.
ANSWERABLE_ACTIVITIES = ("clarify", "limit")
#: Keys that SETTLE a dialog (pick, confirm, refuse) — as opposed to the
#: ones that only move its cursor or toggle a choice.
SETTLING_KEYS = frozenset(
    ["Enter", "Escape", "y", "n"] + [str(n) for n in range(1, 10)]
)
#: How long a dialog id that was just answered stays answered: a second
#: settling answer pinned to the same id inside this window is refused. The
#: CLI may not have redrawn yet, so "the id is still on screen" is not news;
#: past the window it is (the same prompt asked again, or the key was lost).
#: The UI's answer strip releases its "answered" latch on the same clock.
ANSWERED_HOLD_S = 4.0

# C0 controls (incl. ESC, CR/LF, TAB), DEL and C1 controls: typed literally
# they would submit, escape or drive the terminal instead of filling a field.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Pause after literal text before the first key, so the TUI does not read the
# text+key burst as one paste (same reason _send_to_agent pauses before
# Enter); and between keys, so a dialog sees discrete presses.
_TEXT_KEY_GAP_S = 0.15
_KEY_GAP_S = 0.08
_TMUX_TIMEOUT = 10


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


def parse_max_chars(raw) -> Tuple[int, Optional[str]]:
    """``(max_chars, error)`` from a query value: default when absent, clamped
    to :data:`MAX_MAX_CHARS`, an error for a non-integer or non-positive one."""
    if raw is None or raw == "":
        return DEFAULT_MAX_CHARS, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0, "max_chars must be an integer"
    if value <= 0:
        return 0, "max_chars must be positive"
    return min(value, MAX_MAX_CHARS), None


def tail(text: str, max_chars: int) -> Tuple[str, bool]:
    """``(text, truncated)`` keeping the LAST ``max_chars`` characters."""
    text = text or ""
    if len(text) <= max_chars:
        return text, False
    return text[-max_chars:], True


def capture_screen(name: str) -> Tuple[Optional[str], Optional[str]]:
    """The visible screen of tmux session ``name`` → ``(text, error)``.

    ``capture-pane -p -J`` with no ``-S``: the current screen only (not the
    scrollback), wrapped lines joined. Trailing blank rows are dropped.
    """
    out = _server()._run_capped(
        ["tmux", "capture-pane", "-p", "-J", "-t", name],
        capture_output=True,
        text=True,
        timeout=_TMUX_TIMEOUT,
    )
    if out.returncode != 0:
        return None, (out.stderr or "").strip() or "capture failed"
    return (out.stdout or "").rstrip("\n") + "\n", None


def capture_scrollback(name: str) -> Tuple[Optional[str], Optional[str]]:
    """The whole scrollback of tmux session ``name`` (``-S -``), the same
    capture ``/history`` falls back to when there is no transcript."""
    out = _server()._run_capped(
        ["tmux", "capture-pane", "-p", "-J", "-t", name, "-S", "-"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if out.returncode != 0:
        return None, (out.stderr or "").strip() or "capture failed"
    return (out.stdout or "").rstrip("\n") + "\n", None


def clean_answer_text(text: str) -> str:
    """Strip every control character so the text can only fill a field."""
    return _CONTROL_CHARS.sub("", text or "")


def parse_answer(payload) -> Tuple[str, List[str], Optional[str]]:
    """Validate an ``/answer`` body → ``(text, keys, error)``.

    ``text`` (optional str, ≤ :data:`MAX_ANSWER_TEXT` chars, control
    characters stripped) is typed literally; ``keys`` (optional list of
    :data:`ANSWER_KEYS` names, ≤ :data:`MAX_ANSWER_KEYS`) are pressed after it,
    in order. At least one of them must carry something.
    """
    payload = payload if isinstance(payload, dict) else {}
    raw_text = payload.get("text", "")
    if raw_text is None:
        raw_text = ""
    if not isinstance(raw_text, str):
        return "", [], "text must be a string"
    if len(raw_text) > MAX_ANSWER_TEXT:
        return "", [], "text is too long (max %d characters)" % MAX_ANSWER_TEXT
    text = clean_answer_text(raw_text)
    raw_keys = payload.get("keys", [])
    if raw_keys is None:
        raw_keys = []
    if not isinstance(raw_keys, list):
        return "", [], "keys must be a list"
    if len(raw_keys) > MAX_ANSWER_KEYS:
        return "", [], "too many keys (max %d)" % MAX_ANSWER_KEYS
    keys: List[str] = []
    for k in raw_keys:
        if not isinstance(k, str) or k not in ANSWER_KEYS:
            return (
                "",
                [],
                "key not allowed: %r (allowed: %s)"
                % (k, ", ".join(sorted(ANSWER_KEYS))),
            )
        keys.append(k)
    if not text and not keys:
        return "", [], "nothing to send: give text and/or keys"
    return text, keys, None


def parse_answer_meta(payload) -> Tuple[Optional[str], str, Optional[str]]:
    """``/answer``'s optional guard fields → ``(dialog_id, by, error)``.

    ``dialog_id`` (a string from ``GET /dialog``) is None when absent; ``by``
    is ``"agent"`` unless the body says ``"user"``."""
    payload = payload if isinstance(payload, dict) else {}
    dialog = payload.get("dialog_id")
    if dialog is not None:
        if not isinstance(dialog, str) or not dialog.strip():
            return None, "", "dialog_id must be a non-empty string"
        if len(dialog) > MAX_DIALOG_ID:
            return None, "", "dialog_id is too long"
        dialog = dialog.strip()
    by = payload.get("by")
    if by is None:
        by = "agent"
    if by not in ANSWER_BY:
        return None, "", "by must be one of: %s" % ", ".join(ANSWER_BY)
    return dialog, by, None


# --------------------------------------------------------------------------- #
# One answer at a time, once per dialog                                        #
# --------------------------------------------------------------------------- #
_ANSWER_LOCKS: Dict[str, threading.Lock] = {}
_ANSWER_LOCKS_GUARD = threading.Lock()
#: title -> (dialog id, monotonic time) of the last settling answer.
_ANSWERED: Dict[str, Tuple[str, float]] = {}
_ANSWERED_MAX = 1024


def answer_lock(title: str) -> threading.Lock:
    """The per-session lock ``/answer`` holds from its screen read through
    its keys: two clicks on the same prompt (the rail strip and the bell, a
    double click, a person and an orchestrator) must not both pass the
    dialog check before either has typed."""
    with _ANSWER_LOCKS_GUARD:
        lock = _ANSWER_LOCKS.get(title)
        if lock is None:
            lock = _ANSWER_LOCKS[title] = threading.Lock()
        return lock


def settles(keys: List[str]) -> bool:
    """Whether pressing ``keys`` answers a dialog (vs. only navigating it)."""
    return any(k in SETTLING_KEYS for k in keys)


def note_dialog_seen(title: str, dialog_id: Optional[str]) -> None:
    """A read of ``title``'s screen found ``dialog_id`` up: once a DIFFERENT
    dialog has been seen, the one answered before it is history, and an
    identical prompt asked again is answerable at once."""
    if not dialog_id:
        return
    last = _ANSWERED.get(title)
    if last is not None and last[0] != dialog_id:
        _ANSWERED.pop(title, None)


def answered_recently(title: str, dialog_id: str) -> bool:
    """Whether ``dialog_id`` got a settling answer less than
    :data:`ANSWERED_HOLD_S` ago (and no other dialog was seen since)."""
    last = _ANSWERED.get(title)
    return (
        last is not None
        and last[0] == dialog_id
        and time.monotonic() - last[1] < ANSWERED_HOLD_S
    )


def note_answered(title: str, dialog_id: str) -> None:
    if len(_ANSWERED) >= _ANSWERED_MAX:
        _ANSWERED.clear()
    _ANSWERED[title] = (dialog_id, time.monotonic())


def current_dialog(name: str, program: str) -> Tuple[Optional[dict], Optional[str]]:
    """The dialog on tmux session ``name``'s visible screen as the ``/dialog``
    body (see :func:`backend.providers.dialogs.describe`) → ``(body, error)``.

    The session's provider parses it (:meth:`parse_dialog`); a provider with
    no parser, or one that raises, gives the unparsed body. ``error`` is the
    capture's failure (the pane is gone or tmux refused)."""
    from backend import providers
    from backend.providers import dialogs

    screen, err = capture_screen(name)
    if err is not None:
        return None, err
    try:
        parsed = providers.resolve(program or "").parse_dialog(screen)
    except Exception:  # noqa: BLE001 — a parser bug reads as "unparsed"
        parsed = None
    if not isinstance(parsed, dict):
        parsed = None
    return dialogs.describe(parsed, screen), None


def send_answer(name: str, text: str, keys: List[str]) -> bool:
    """Type ``text`` literally (no Enter), then press each key in order.

    False when the tmux session is gone or any send errored (keys after a
    failure are not sent — a half-driven dialog should stop, not guess).
    """
    srv = _server()
    if not name:
        return False
    exists = (
        srv._run_capped(
            ["tmux", "has-session", "-t=" + name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TMUX_TIMEOUT,
        ).returncode
        == 0
    )
    if not exists:
        return False
    # ``--``: an answer that starts with '-' is text, never a tmux flag (it
    # used to be parsed as one — rejected, or silently dropped while the keys
    # still went out). The lock keeps another typer out of the text-to-keys gap.
    with _agent_sessions._typing_lock(name):
        if text:
            typed = srv._run_capped(
                ["tmux", "send-keys", "-t", name, "-l", "--", text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TMUX_TIMEOUT,
            )
            if typed.returncode != 0:
                return False
            if keys:
                time.sleep(_TEXT_KEY_GAP_S)
        for i, key in enumerate(keys):
            if i:
                time.sleep(_KEY_GAP_S)
            pressed = srv._run_capped(
                ["tmux", "send-keys", "-t", name, key],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TMUX_TIMEOUT,
            )
            if pressed.returncode != 0:
                return False
    return True
