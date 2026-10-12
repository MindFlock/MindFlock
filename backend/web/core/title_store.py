"""Load/persist for the small ``title -> value`` JSON stores in the config dir.

:mod:`aliases`, :mod:`hidden_sessions`, :mod:`person_sessions`,
:mod:`ownership` (owners) and :mod:`shared_branch` each keep one such map:
loaded once into a module global, mutated under the module's lock, and
rewritten whole after every change. They share these two functions; each keeps
its own global, lock and ``clean`` rule (what a valid entry is).

The write is atomic (:func:`backend.fsutil.atomic_write_json`), so a crash or a
full disk mid-write leaves the previous file, never a truncated one. The bytes
are what ``json.dump(data, f)`` always wrote here.
"""

from __future__ import annotations

from typing import Callable, Dict

from backend import log
from backend.fsutil import atomic_write_json, read_json


def load(path: Callable[[], str], clean: Callable[[dict], Dict]) -> Dict:
    """``clean(document)`` for the file at ``path()``; ``{}`` when the path
    cannot be resolved, the file is missing/unreadable/corrupt, or it does not
    hold a JSON object."""
    try:
        p = path()
    except OSError:
        return {}
    d = read_json(p, None, expect=dict)
    return clean(d) if d is not None else {}


def persist(path: Callable[[], str], data: Dict, what: str) -> None:
    """Atomically rewrite ``path()`` with ``data``. Never raises an OSError: a
    failed save is logged (``failed to save <what>``) and the old file stays."""
    try:
        atomic_write_json(
            path(), data or {}, indent=None, newline=False, ensure_ascii=True
        )
    except OSError as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to save %s: %v", what, err)
