"""Cap glibc's malloc arenas for the long-running server process.

glibc gives every thread that allocates under contention its own malloc arena
(up to 8 per core), and memory freed inside an arena mostly stays mapped in
it. The server runs its blocking work (git/tmux probes, transcript reads, the
usage scan) on worker threads, so over a few minutes each pool thread grew
its own arena of freed-but-kept memory: the live server sat at ~1 GB RSS,
~700-900 MB of it in ~30 thread arenas of ~25-55 MB each, while a sandboxed
copy's traced Python heap was ~70-115 MB. Two arenas let those threads reuse
each other's free memory; the GIL already serializes them, so the extra lock
contention costs nothing measurable. Sandboxed (copied state, real
transcripts, UI-like polling): 616 MB at 10 min and 749 MB at 26 min, still
climbing, became a flat ~265 MB.

Set with ``mallopt`` before the server starts its threads (arenas are made
lazily, so a cap set this early applies to all of them) rather than the
``MALLOC_ARENA_MAX`` environment variable, which would leak into every agent
shell the server launches. An explicit ``MALLOC_ARENA_MAX`` /
``GLIBC_TUNABLES`` in the environment wins: glibc already applied it, and we
leave it alone. No-op off Linux/glibc. Never raises.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

#: ``mallopt`` parameter number of ``M_ARENA_MAX`` (glibc ``malloc.h``).
M_ARENA_MAX = -8

#: The cap: the main arena plus one for every worker thread to share.
ARENA_MAX = 2


def cap_arenas(limit: int = ARENA_MAX) -> Optional[int]:
    """Apply the arena cap; returns the cap applied, or None when skipped
    (not glibc, or the environment already chose one)."""
    if not sys.platform.startswith("linux"):
        return None
    if os.environ.get("MALLOC_ARENA_MAX") or "arena_max" in os.environ.get(
        "GLIBC_TUNABLES", ""
    ):
        return None
    try:
        import ctypes

        libc = ctypes.CDLL(None)
        gnu_get_libc_version = libc.gnu_get_libc_version  # glibc only (not musl)
        del gnu_get_libc_version
        ok = libc.mallopt(ctypes.c_int(M_ARENA_MAX), ctypes.c_int(limit))
    except Exception:  # noqa: BLE001 — tuning is best-effort, never fatal
        return None
    return limit if ok == 1 else None
