"""Sessions a feature owns and the rail must not list (``hidden_by``).

A Verify run is a real session — it needs a worktree and an agent that can run
commands — but it is not the user's work: it is a window you open to watch a
checklist for two minutes and close again. The sidebar, the window numbers and
the prompt-target pickers leave it out. Until now they recognised one by its
TITLE (``verify-<plan>-<sha>``), which meant any session a person happened to
name ``verify-foo`` vanished from the rail too, with no pane to type into and
no way to see why.

This is the explicit flag instead: ``title -> who``, where ``who`` names the
feature that owns the session (:data:`KINDS`, today only ``"verify"``). The
create route records it when the caller passes ``hidden_by`` (which is what
``run_test_plan`` does), every ``/api/instances`` row carries it as
``hidden_by`` (``""`` for an ordinary session), and the frontend hides on the
field, not the name.

Web metadata, NOT engine state: the engine's Instance JSON is byte-exact and
pinned by goldens, and this is a display concern of the web layer — the same
reasoning that keeps the display aliases in :mod:`backend.web.core.aliases`,
whose on-disk pattern this copies. Persisted as ``<config dir>/
session_hidden.json``; one process writes, so an in-memory dict loaded once is
safe.

:func:`backfill_verify` covers the sessions that existed before the flag did:
a verify session a checklist still names as one of its runs is flagged at
startup, so upgrading does not suddenly list every old run on the rail.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, Iterable, Optional

from backend import config, log

#: Features that may own a hidden session. The create route 400s any other
#: value, so a typo cannot quietly hide a session forever.
KINDS = ("verify",)

#: The title prefix ``run_test_plan`` builds verify sessions from — kept here
#: ONLY for the startup backfill of sessions created before the flag existed.
#: Nothing else may decide anything from it.
LEGACY_VERIFY_PREFIX = "verify-"

_LOCK = threading.Lock()
_HIDDEN: Optional[Dict[str, str]] = None  # title -> owning feature


def _path() -> str:
    return os.path.join(config.GetConfigDir(), "session_hidden.json")


def _load() -> Dict[str, str]:
    global _HIDDEN
    if _HIDDEN is None:
        try:
            with open(_path()) as f:
                d = json.load(f)
            _HIDDEN = (
                {str(k): str(v) for k, v in d.items() if v in KINDS}
                if isinstance(d, dict)
                else {}
            )
        except (OSError, ValueError):
            _HIDDEN = {}
    return _HIDDEN


def _persist() -> None:
    try:
        with open(_path(), "w") as f:
            json.dump(_HIDDEN or {}, f)
    except OSError as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to save hidden sessions: %v", err)


def validate(value) -> Optional[str]:
    """Why a create payload's ``hidden_by`` is unacceptable, or ``None``.

    Absent, ``None`` and ``""`` all mean "an ordinary session". Anything else
    must be one of :data:`KINDS`, as a string — a boolean or a list is a caller
    bug, and refusing it is the only way the caller finds out.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in KINDS:
        return "hidden_by must be one of %s (got %r)" % (", ".join(KINDS), value)
    return None


def set_hidden_by(title: str, who: str) -> None:
    """Record who owns ``title`` (``""`` = an ordinary session: forget it)."""
    title = str(title or "").strip()
    if not title:
        return
    who = str(who or "")
    if who and who not in KINDS:
        return
    with _LOCK:
        hidden = _load()
        if who:
            if hidden.get(title) == who:
                return
            hidden[title] = who
        elif title not in hidden:
            return
        else:
            hidden.pop(title, None)
        _persist()


def hidden_by(title: str) -> str:
    """The feature that owns ``title``, or ``""``. Never raises — a row must
    never fail to build over a display flag."""
    try:
        with _LOCK:
            return _load().get(str(title or "")) or ""
    except Exception:  # noqa: BLE001
        return ""


def drop(title: str) -> None:
    """Forget a deleted session (missing is fine). Titles are reused, so a
    namesake must not come back hidden."""
    with _LOCK:
        hidden = _load()
        if title in hidden:
            hidden.pop(title, None)
            _persist()


def all_hidden() -> Dict[str, str]:
    with _LOCK:
        return dict(_load())


def backfill_verify(live_titles: Iterable[str], plan_sessions: Iterable[str]) -> int:
    """Flag the verify sessions that predate the flag; returns how many.

    A session counts when it is live, its title carries the legacy prefix AND
    a checklist names it as one of its runs — the title alone is exactly the
    heuristic this store replaces, so it is not trusted on its own. Idempotent;
    runs once per server start.
    """
    plans = {str(s) for s in plan_sessions if s}
    n = 0
    for title in live_titles:
        t = str(title or "")
        if not t.startswith(LEGACY_VERIFY_PREFIX) or t not in plans:
            continue
        if hidden_by(t):
            continue
        set_hidden_by(t, "verify")
        n += 1
    return n
