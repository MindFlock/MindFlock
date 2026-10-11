"""The New-session dialog's "Base branch" picker: ``GET /api/repos/branches``.

One ``git for-each-ref`` per repo, answered from a 10-second memo so the
picker (which asks again on every folder change) never queues subprocesses
behind a slow disk. Local branches first (the checked-out one at the top),
then remote-tracking refs, capped at :data:`MAX_BRANCHES` — a monorepo with
thousands of stale remote branches would otherwise ship a <select> nobody
can scroll.

The answer is only ever a MENU: the dialog sends the chosen name back as
``base_ref``/``base_branch`` on ``POST /api/instances``, and
:mod:`backend.web.core.session_create` resolves and validates it there, so
nothing here has to be right about what the branch points at.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

#: Most branches one answer carries (local first, so remotes are what get cut).
MAX_BRANCHES = 200
#: How long an answer is reused for the same folder.
CACHE_TTL = 10.0

_CACHE: Dict[str, Tuple[float, dict]] = {}
_LOCK = threading.Lock()


def _empty(path: str) -> dict:
    return {"path": path, "is_git": False, "current": "", "default": "", "branches": []}


def _git(path: str, *args: str) -> Tuple[int, str]:
    try:
        cp = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return cp.returncode, cp.stdout


def _default_branch(path: str, local: List[str]) -> str:
    """What the server would cut from when nothing is chosen: origin's HEAD
    when the remote says, else main/master if one exists, else the checked-out
    branch (callers fall back to ``current``)."""
    rc, out = _git(path, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.strip():
        name = out.strip()
        if name.startswith("origin/"):
            name = name[len("origin/") :]
        if name in local:
            return name
    for name in ("main", "master"):
        if name in local:
            return name
    return ""


def _list(path: str) -> dict:
    full = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(full):
        return _empty(full)
    rc, top = _git(full, "rev-parse", "--show-toplevel")
    if rc != 0:
        return _empty(full)
    rc, head = _git(full, "symbolic-ref", "-q", "--short", "HEAD")
    current = head.strip() if rc == 0 else ""
    rc, out = _git(
        full,
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname)",
        "refs/heads",
        "refs/remotes",
    )
    if rc != 0:
        return _empty(full)
    local: List[str] = []
    remote: List[str] = []
    for line in out.splitlines():
        ref = line.strip()
        if ref.startswith("refs/heads/"):
            local.append(ref[len("refs/heads/") :])
        elif ref.startswith("refs/remotes/"):
            name = ref[len("refs/remotes/") :]
            # ``origin/HEAD`` is a pointer, not a branch anyone cuts from.
            if name.endswith("/HEAD"):
                continue
            remote.append(name)
    # Current first, then the rest newest-first — the picker's top rows are
    # the ones somebody is most likely to want.
    if current in local:
        local.remove(current)
        local.insert(0, current)
    branches: List[dict] = [
        {"name": n, "remote": False, "current": n == current} for n in local
    ]
    branches += [{"name": n, "remote": True, "current": False} for n in remote]
    return {
        "path": full,
        "is_git": True,
        "current": current,
        "default": _default_branch(full, local) or current,
        "branches": branches[:MAX_BRANCHES],
        "truncated": len(branches) > MAX_BRANCHES,
    }


def list_branches(path: str, now: Optional[float] = None) -> dict:
    """Branches of the repo at ``path`` (see module docstring); memoised
    :data:`CACHE_TTL` seconds per resolved folder. A blank path or a folder
    that is not a git repo answers ``is_git: False`` with an empty list —
    never an error, the picker simply has nothing to offer."""
    text = str(path or "").strip()
    if not text:
        return _empty("")
    try:
        key = os.path.realpath(os.path.expanduser(text))
    except (OSError, ValueError):
        return _empty(text)
    t = time.monotonic() if now is None else now
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and t - hit[0] < CACHE_TTL:
            return hit[1]
    answer = _list(key)
    with _LOCK:
        _CACHE[key] = (t, answer)
        if len(_CACHE) > 64:
            # Oldest out — a bounded memo, not a registry of every folder typed.
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(oldest, None)
    return answer


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
