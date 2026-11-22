"""One worktree change at a time per repository.

Why it exists: an orchestrator that spawned several workers in one batch lost
some of them. Each worker's Start runs ``git worktree add`` in the SAME
repository, and two of those at the same moment can fail: ``worktree add``
reads every other worktree's admin folder (``.git/worktrees/<name>/``) to
check its branch is not checked out there, and a sibling's folder may be
half-written at that instant::

    fatal: failed to read .git/worktrees/orch-w4_…/commondir: Success

(about one Setup in ten in a burst of ten, on WSL). So every command that
adds or removes a worktree takes :func:`worktree_lock` for its repository:
a ``threading`` lock per repository inside this process, and an advisory
file lock (``<git common dir>/mindflock-worktree.lock``) across processes —
the ticket pipeline is a separate process that makes worktrees too.

The key is the repository's common git dir, so the repository's root and
any of its worktrees share one lock, and two repositories never wait for
each other. A path git can't read falls back to an in-process lock on the
path itself. Stdlib only (plus :mod:`backend.compat`'s cross-platform
``flock``).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
from typing import Dict, Iterator, Optional

from backend import compat

__all__ = ["lock_key", "worktree_lock", "LOCK_FILE"]

#: The advisory lock file, inside the repository's common git dir (git
#: ignores files it does not know there).
LOCK_FILE = "mindflock-worktree.lock"

_GUARD = threading.Lock()
_LOCKS: Dict[str, threading.RLock] = {}
#: Per thread: how deep it holds each key. Only the outermost take flocks: a
#: nested one opening the lock file again would wait for the first, i.e. for
#: itself.
_HELD = threading.local()


def _common_dir(path: str) -> Optional[str]:
    """``path``'s common git dir (absolute, resolved), or None."""
    try:
        res = subprocess.run(
            ["git", "-C", path, "rev-parse", "--git-common-dir"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = res.stdout.decode("utf-8", "replace").strip()
    if res.returncode != 0 or not out:
        return None
    if not os.path.isabs(out):
        out = os.path.join(path, out)
    return os.path.realpath(out)


def lock_key(path: str) -> str:
    """What :func:`worktree_lock` keys on: the common git dir of ``path``'s
    repository, else ``path`` itself (resolved)."""
    return _common_dir(path) or os.path.realpath(path or ".")


def _thread_lock(key: str) -> threading.RLock:
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


@contextlib.contextmanager
def worktree_lock(repo_path: str) -> Iterator[None]:
    """Hold ``repo_path``'s worktree lock for the block. Blocking. A file lock
    that can't be taken (read-only ``.git``, an odd filesystem) degrades to
    the in-process lock alone rather than failing the worktree command.
    Reentrant within a thread (``Cleanup`` holds it and calls ``Prune``)."""
    common = _common_dir(repo_path)
    key = common or os.path.realpath(repo_path or ".")
    depth: Dict[str, int] = getattr(_HELD, "depth", None) or {}
    _HELD.depth = depth
    with _thread_lock(key):
        if depth.get(key):
            depth[key] += 1
            try:
                yield
            finally:
                depth[key] -= 1
            return
        depth[key] = 1
        fd: Optional[int] = None
        if common and os.path.isdir(common):
            try:
                fd = os.open(os.path.join(common, LOCK_FILE), os.O_RDWR | os.O_CREAT)
                compat.flock(fd, compat.LOCK_EX)
            except OSError:
                if fd is not None:
                    os.close(fd)
                fd = None
        try:
            yield
        finally:
            depth.pop(key, None)
            if fd is not None:
                try:
                    compat.flock(fd, compat.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)
