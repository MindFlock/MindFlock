"""Load/persist for the small ``title -> value`` JSON stores in the config dir.

:mod:`aliases`, :mod:`hidden_sessions`, :mod:`person_sessions`,
:mod:`ownership` (owners) and :mod:`shared_branch` each keep one such map:
loaded once into a module global, mutated under the module's lock, and
rewritten whole after every change. They share :func:`load` and
:func:`persist`; each keeps its own global, lock and ``clean`` rule (what a
valid entry is).

The write is atomic (:func:`backend.fsutil.atomic_write_json`), so a crash or a
full disk mid-write leaves the previous file, never a truncated one. The bytes
are what ``json.dump(data, f)`` always wrote here.

**Shared stores** — :class:`SharedMap` is the same map for a file that more
than one MindFlock process writes (two servers on one ``~/.mindflock``: the
service and a dev server). Loaded once and rewritten whole, each process's
save used to overwrite the other's records, and neither saw the other's
change until it restarted. A :class:`SharedMap` instead

* re-reads the file whenever it changed on disk (inode, size, mtime), so a
  record another process wrote is seen on the next read, no restart;
* saves read-merge-write under an inter-process lock (``<file>.lock``,
  :func:`file_lock`): only the keys THIS process added, changed or removed
  since it last synced are applied to what is on disk now, so it never drops
  (or resurrects) a key it did not touch;
* still writes atomically (tmp + ``os.replace``).
"""

from __future__ import annotations

import copy
import json
import os
import threading
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, Optional, Set, Tuple

from backend import compat, log
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
        _write(path(), data)
    except OSError as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to save %s: %v", what, err)


def _write(p: str, data: Dict) -> None:
    atomic_write_json(p, data or {}, indent=None, newline=False, ensure_ascii=True)


@contextmanager
def file_lock(path: str) -> Iterator[bool]:
    """Hold the exclusive inter-process lock of the store at ``path`` (an
    advisory ``flock`` on ``<path>.lock`` via :mod:`backend.compat`, so it
    works on Windows too; the kernel drops it if the holder dies). The lock
    sits on a sidecar that is never replaced: the store itself is swapped by
    ``os.replace``, so a lock on its inode would protect nothing. Yields
    whether the lock was taken: a lock file that cannot be opened (read-only
    dir) is best effort, never an error. NOT reentrant: one holder per
    process at a time (:class:`SharedMap` counts its own depth)."""
    fd: Optional[int] = None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
        compat.flock(fd, compat.LOCK_EX)
    except OSError:
        if fd is not None:
            os.close(fd)
        fd = None
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)  # closing the fd releases the flock


def _signature(p: str) -> Optional[Tuple[int, int, int]]:
    """What changes whenever the file does: an ``os.replace`` brings a new
    inode, an in-place write a new size or mtime. None when missing."""
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


_MISSING = object()


class SharedMap:
    """A ``title -> record`` JSON map shared by every process on one config
    dir (see the module docstring). Use it under the owning module's lock:

    * :meth:`data` — the live dict, re-read when the file changed on disk;
    * :meth:`transaction` — a context manager for a mutation: the inter-
      process lock is held, the dict is fresh from disk, and what the body
      changed is written when it exits (nothing when it changed nothing; and
      a body that raises leaves the store as it was);
    * :meth:`persist` — write what was changed in :meth:`data`'s dict since
      the last sync, merged onto the file's current content (for callers
      that mutate the dict directly, e.g. ``core.rename_stores``).

    The path is resolved on every call (keyed by path, so a per-test store
    starts over)."""

    def __init__(
        self, path: Callable[[], str], clean: Callable[[dict], Dict], what: str
    ) -> None:
        self._path_fn = path
        self._clean = clean
        self._what = what
        self._mutex = threading.RLock()
        self._p: Optional[str] = None
        self._data: Dict = {}
        self._base: Dict = {}
        self._sig: Optional[Tuple[int, int, int]] = None
        self._depth = 0

    # -- internals -------------------------------------------------------- #
    def _resolve(self) -> str:
        try:
            return self._path_fn() or ""
        except OSError:
            return ""

    def _read(self, p: str) -> Dict:
        d = read_json(p, None, expect=dict)
        return self._clean(d) if d is not None else {}

    def _adopt(self, p: str, recs: Dict, sig) -> None:
        """Make ``recs`` (fresh from disk) the synced state. In place: a
        caller holding the dict keeps seeing the store."""
        self._p = p
        self._data.clear()
        self._data.update(recs)
        self._base = copy.deepcopy(recs)
        self._sig = sig

    @contextmanager
    def _locked(self, p: str) -> Iterator[None]:
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        with file_lock(p):
            self._depth = 1
            try:
                yield
            finally:
                self._depth = 0

    # -- API ---------------------------------------------------------------- #
    def path(self) -> str:
        """The resolved path ("" when it cannot be)."""
        return self._resolve()

    def reset(self) -> None:
        """Forget the cache (what a fresh process would have)."""
        with self._mutex:
            self._p = None
            self._data = {}
            self._base = {}
            self._sig = None

    def data(self) -> Dict:
        """The records (the live dict: copy what you hand out). Re-read from
        disk when the path or the file changed since the last look."""
        with self._mutex:
            p = self._resolve()
            if not p:
                if self._p != "":
                    self._adopt("", {}, None)
                return self._data
            # Stat BEFORE reading: a write landing in between makes the next
            # call read again, never leaves new content marked as old.
            sig = _signature(p)
            if p != self._p or sig != self._sig or sig is None and self._data:
                self._adopt(p, self._read(p), sig)
            return self._data

    def persist(self) -> None:
        """Merge this process's changes since the last sync into the file
        (under the inter-process lock) and write it atomically. Keys this
        process never touched stay as the file has them. Never raises an
        OSError: a failed save is logged and the changes are kept for the
        next save."""
        with self._mutex:
            p = self._p if self._p is not None else self._resolve()
            if not p:
                return
            with self._locked(p):
                disk = self._read(p)
                dirty = False
                for k in self._base:
                    if k not in self._data and k in disk:
                        del disk[k]
                        dirty = True
                for k, v in self._data.items():
                    if self._base.get(k, _MISSING) != v and disk.get(k, _MISSING) != v:
                        disk[k] = copy.deepcopy(v)
                        dirty = True
                if dirty:
                    try:
                        _write(p, disk)
                    except OSError as err:
                        if log.ErrorLog is not None:
                            log.ErrorLog.Printf(
                                "failed to save %s: %v", self._what, err
                            )
                        return
                self._adopt(p, disk, _signature(p))

    @contextmanager
    def transaction(self) -> Iterator[Dict]:
        """``with store.transaction() as recs:`` — mutate ``recs`` (fresh from
        disk, under the inter-process lock); the change is saved on exit."""
        with self._mutex:
            p = self._resolve()
            if not p:
                self._adopt("", {}, None)
                yield self._data
                return
            with self._locked(p):
                self._adopt(p, self._read(p), _signature(p))
                try:
                    yield self._data
                except BaseException:
                    self._adopt(p, self._base, self._sig)  # roll back
                    raise
                self.persist()


def state_titles(store_path: str) -> Set[str]:
    """The session titles in the ``state.json`` next to ``store_path`` (the
    engine's file, which every server on this config dir shares). A server
    adopts sessions another server created or renamed only on its next state
    sync (a few seconds), so a title missing from its own engine may be one
    that exists: a prune checks here before dropping a record. Empty when
    unreadable. Parsed again only when the file changed (prunes run every
    instances tick)."""
    global _STATE_TITLES
    p = os.path.join(os.path.dirname(store_path) or ".", "state.json")
    sig = _signature(p)
    if sig is None:
        return set()
    if _STATE_TITLES[0] == (p, sig):
        return set(_STATE_TITLES[1])
    try:
        d = read_json(p, None, expect=dict)
    except Exception:  # noqa: BLE001
        return set()
    out: Set[str] = set()
    inst = (d or {}).get("instances")
    if isinstance(inst, str):  # stored as a raw JSON string by some versions
        try:
            inst = json.loads(inst)
        except ValueError:
            inst = None
    for r in inst if isinstance(inst, list) else ():
        if isinstance(r, dict) and r.get("title"):
            out.add(str(r["title"]))
    _STATE_TITLES = ((p, sig), frozenset(out))
    return out


#: ``((path, signature), titles)`` of the last ``state.json`` parsed.
_STATE_TITLES: Tuple[Optional[tuple], frozenset] = (None, frozenset())
