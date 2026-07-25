"""Per-session dev-server port allocation (roadmap O4).

Parallel worktrees fight over hardcoded dev-server ports (":3000 is taken").
Each session gets a deterministic block of :data:`BLOCK_SIZE` consecutive
ports, derived from an FNV-1a hash of its title and linear-probed past blocks
already held by other live sessions. The allocation is persisted in its own
JSON file (``~/.mindflock/ports.json``, same single-purpose-file pattern as
``prompt_queues.json``) so a block survives server restarts and stays stable
for the life of the session.

The block is surfaced two ways:

* env vars injected into the agent's tmux session at launch (``PORT`` =
  block base, plus ``MINDFLOCK_PORT_BASE`` / ``MINDFLOCK_PORT_COUNT`` for
  tools that want the whole block) — see :func:`env_for`;
* ``ports`` on ``GET /api/instances`` so the UI can render a clickable
  preview URL per session.

Deliberately no bind-probing of the host: dev servers come and go, so a
point-in-time bind check is stale the moment it returns. Uniqueness across
sessions — the actual failure mode reported by users — is guaranteed by the
persisted map.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, Optional

from backend.config.config import GetConfigDir

_FileName = "ports.json"

# 3100, 3110, ... 9090: 600 blocks of 10 ports, clear of the common
# hardcoded dev ports (3000, 5173, 8000, 8080 all fall outside the pool
# except 8000/8080 which sit between blocks' *bases* — a session's env
# steers its servers onto the block instead).
BASE = 3100
BLOCK_SIZE = 10
BLOCKS = 600

_lock = threading.Lock()


def _path() -> str:
    """Path to the allocation store.

    Honors ``$MINDFLOCK_PORTS_FILE`` (tests point it at a tmp file);
    otherwise ``<config dir>/ports.json``.
    """
    env = os.environ.get("MINDFLOCK_PORTS_FILE")
    if env:
        return env
    return os.path.join(GetConfigDir(), _FileName)


def _load() -> Dict[str, int]:
    try:
        with open(_path(), encoding="utf-8") as f:
            raw = json.load(f)
        return {
            str(k): int(v)
            for k, v in (raw or {}).items()
            if isinstance(v, (int, float))
        }
    except Exception:  # noqa: BLE001 — missing/corrupt file = empty map
        return {}


def _save(data: Dict[str, int]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _fnv1a(s: str) -> int:
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def base_for(title: str) -> int:
    """The deterministic (pre-probing) block base for a title."""
    return BASE + (_fnv1a(title) % BLOCKS) * BLOCK_SIZE


def allocate(title: str) -> int:
    """Return ``title``'s port-block base, allocating one if needed.

    Deterministic hash slot first, then linear probing past blocks held by
    other sessions. Idempotent: an existing allocation is returned as-is.
    """
    with _lock:
        data = _load()
        if title in data:
            return data[title]
        taken = set(data.values())
        slot = _fnv1a(title) % BLOCKS
        for i in range(BLOCKS):
            port = BASE + ((slot + i) % BLOCKS) * BLOCK_SIZE
            if port not in taken:
                data[title] = port
                _save(data)
                return port
        # 600 live sessions — reuse the hash slot rather than fail.
        port = BASE + slot * BLOCK_SIZE
        data[title] = port
        _save(data)
        return port


def release(title: str) -> None:
    """Free ``title``'s block (called on session delete). No-op if absent."""
    with _lock:
        data = _load()
        if data.pop(title, None) is not None:
            _save(data)


def get(title: str) -> Optional[int]:
    """The allocated block base for ``title``, or None."""
    with _lock:
        return _load().get(title)


def prune(live_titles) -> None:
    """Drop allocations whose session no longer exists."""
    live = set(live_titles)
    with _lock:
        data = _load()
        stale = [t for t in data if t not in live]
        for t in stale:
            del data[t]
        if stale:
            _save(data)


def env_for(title: str, port: Optional[int] = None) -> Dict[str, str]:
    """The env vars a session's processes see for its port block."""
    p = port if port is not None else allocate(title)
    return {
        "PORT": str(p),
        "MINDFLOCK_PORT_BASE": str(p),
        "MINDFLOCK_PORT_COUNT": str(BLOCK_SIZE),
    }
