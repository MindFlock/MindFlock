"""Which person created each session (``title -> person id``).

Sessions created from now on record the person the create request was for
(:func:`backend.web.core.auth.request_person`: the relaying device's person,
else this device's), and every ``/api/instances`` row carries it as
``person`` (``""`` for a session created before people existed, or one a
peer link made). The ownership / presence layers read it from here.

Web metadata, NOT engine state: the engine's Instance JSON is byte-exact and
pinned by goldens — the same reasoning, and the same on-disk pattern, as
:mod:`backend.web.core.hidden_sessions`. Persisted as ``<config dir>/
session_person.json``; one process writes, so an in-memory dict loaded once
is safe.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, Optional

from backend import config, log

_LOCK = threading.Lock()
_PERSON: Optional[Dict[str, str]] = None  # title -> person id


def _path() -> str:
    return os.path.join(config.GetConfigDir(), "session_person.json")


def _valid(pid: object) -> str:
    from backend.config import settings as _settings

    return _settings.person_id(pid)


def _load() -> Dict[str, str]:
    global _PERSON
    if _PERSON is None:
        try:
            with open(_path()) as f:
                d = json.load(f)
            _PERSON = (
                {str(k): _valid(v) for k, v in d.items() if _valid(v)}
                if isinstance(d, dict)
                else {}
            )
        except (OSError, ValueError):
            _PERSON = {}
    return _PERSON


def _persist() -> None:
    try:
        with open(_path(), "w") as f:
            json.dump(_PERSON or {}, f)
    except OSError as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to save session people: %v", err)


def set_person(title: str, pid: str) -> None:
    """Record that ``title`` was created for ``pid`` (``""`` / not an id:
    forget it — a reused title must not inherit a dead session's person)."""
    title = str(title or "").strip()
    if not title:
        return
    pid = _valid(pid)
    with _LOCK:
        people = _load()
        if pid:
            if people.get(title) == pid:
                return
            people[title] = pid
        elif title not in people:
            return
        else:
            people.pop(title, None)
        _persist()


def person(title: str) -> str:
    """The person ``title`` was created for, or ``""``. Never raises — a row
    must never fail to build over a label."""
    try:
        with _LOCK:
            return _load().get(str(title or "")) or ""
    except Exception:  # noqa: BLE001
        return ""


def drop(title: str) -> None:
    """Forget a deleted session (missing is fine)."""
    with _LOCK:
        people = _load()
        if title in people:
            people.pop(title, None)
            _persist()


def rename(old: str, new: str) -> int:
    """After an id switch (pairing adoption, the sync tie-break): every stamp
    that named ``old`` now names ``new``, so this device's rows agree with its
    own ``/api/person``. Returns how many changed; never raises."""
    old, new = _valid(old), _valid(new)
    if not old or not new or old == new:
        return 0
    try:
        with _LOCK:
            people = _load()
            hits = [t for t, p in people.items() if p == old]
            for t in hits:
                people[t] = new
            if hits:
                _persist()
            return len(hits)
    except Exception:  # noqa: BLE001
        return 0


def all_people() -> Dict[str, str]:
    with _LOCK:
        return dict(_load())
