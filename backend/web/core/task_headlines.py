"""Per-session task headlines: what each session is FOR, as a short verb phrase.

The sidebar's task line used to print the newest prompt verbatim, so it read
"yes", "continue", or MindFlock's own revive prompt as often as it read a task.
:func:`for_session` turns the prompt into a headline (:mod:`backend.task_headline`,
deterministic, no model) and keeps the last MEANINGFUL one per session: when
the newest prompt says nothing about the task ("continue", "commit and push",
a status ping, a MindFlock note), the session keeps showing what it was last
asked to do. The provider only exposes the newest prompt, so that memory lives
here, keyed by title plus the conversation id (a ``/clear`` or a fresh launch
starts a new conversation, whose machine-only first prompt must not inherit
the old headline). It is process memory: after a server restart the fallback
is the session's creation task.

Read by ``server._session_snapshot`` (behind the same ~2.5s probe memo as
``last_prompt``) and by ``remote.merged_instances`` for rows from devices that
predate the field. Nothing here does I/O.
"""

from __future__ import annotations

import functools
import threading
from typing import Dict, Iterable, NamedTuple, Optional, Tuple

from backend import task_headline as _th


class TaskHeadline(NamedTuple):
    """The headline and the prompt it was read from — the row's tooltip, so
    a later "continue" never stands in for the prompt behind a real task."""

    text: Optional[str]
    prompt: Optional[str]


_NONE = TaskHeadline(None, None)

#: title -> (conversation id, headline, source prompt). Bounded: dead titles
#: are dropped by the removal hooks, and the cap is the backstop.
_MEMO: Dict[str, Tuple[str, str, str]] = {}
_MEMO_MAX = 2048
_LOCK = threading.Lock()


@functools.lru_cache(maxsize=4096)
def _read(text: str) -> _th.Headline:
    """The pure reading, memoized by prompt text: every poll of every client
    asks again, and the prompt rarely changes between polls."""
    return _th.headline(text)


def for_session(
    title: str,
    prompt_full: Optional[str],
    prompt: Optional[str] = None,
    task: Optional[str] = None,
    conversation: str = "",
) -> TaskHeadline:
    """The headline to show for one session and the prompt it came from;
    ``(None, None)`` when there is nothing.

    In order: the newest prompt's headline when it names a task (and it is
    remembered, with that prompt); else the headline remembered for this
    conversation; else the creation ``task``'s headline; else the newest
    prompt's own headline ("Continue" beats an empty line).
    """
    text = str(prompt_full or prompt or "").strip()
    newest = _read(text) if text else None
    if newest is not None and newest.meaningful and newest.text:
        _remember(title, conversation or "", newest.text, text)
        return TaskHeadline(newest.text, text)
    with _LOCK:
        memo = _MEMO.get(title)
    if memo is not None and memo[0] == (conversation or ""):
        return TaskHeadline(memo[1], memo[2])
    created = str(task or "").strip()
    if created:
        head = _read(created).text
        if head:
            return TaskHeadline(head, created)
    if newest is not None and newest.text:
        return TaskHeadline(newest.text, text)
    return _NONE


def _remember(title: str, conversation: str, headline: str, prompt: str) -> None:
    with _LOCK:
        if title not in _MEMO and len(_MEMO) >= _MEMO_MAX:
            _MEMO.pop(next(iter(_MEMO)))  # oldest insertion first
        _MEMO[title] = (conversation, headline, prompt)


def drop(title: str) -> None:
    """Forget a removed session, so a namesake starts from its own task."""
    with _LOCK:
        _MEMO.pop(title, None)


def prune(live_titles: Iterable[str]) -> None:
    """Drop local sessions that left the engine. Remote rows (``device::title``)
    are not the engine's to judge and age out through :data:`_MEMO_MAX`."""
    live = set(live_titles or ())
    with _LOCK:
        for title in [t for t in _MEMO if "::" not in t and t not in live]:
            _MEMO.pop(title, None)
