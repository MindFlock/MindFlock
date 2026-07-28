"""Shared live-usage cache primitive for the provider ``usage_api`` modules.

claude_usage_api, codex_usage_api and antigravity_usage_api each expose a
``live_usage()`` with the identical concurrency shape: throttle refreshes to a
TTL, serve the last known-good reading through transient failures for a grace
window, and — critically — run the blocking fetch OUTSIDE the module lock so
one slow probe can't serialize every ``/api/usage`` caller. That invariant
lives here once, so a fix to it can't miss one of the three copies.

The per-module cache dict, lock, TTL, grace and fetch are passed in (resolved
as module globals at the call site) so the modules' monkeypatch-based tests —
which reassign ``api._cache`` and patch ``api._fetch`` — keep working unchanged.
"""

from __future__ import annotations

import time
from typing import Callable, Optional


def serve(
    cache: dict,
    lock,
    ttl: float,
    grace: float,
    fetch: Callable[[], Optional[dict]],
) -> Optional[dict]:
    """Cached live-usage read: fetch at most once per ``ttl``, reuse the last
    good reading for up to ``grace`` seconds through a failed fetch, and run
    ``fetch`` without holding ``lock``.

    ``cache`` is ``{"at": float, "good_at": float, "good": dict|None}`` —
    ``good``/``good_at`` are the last SUCCESSFUL reading and when it landed;
    ``at`` is when a fetch was last ATTEMPTED (throttles retries to ``ttl``
    whether or not the attempt succeeded)."""
    now = time.time()
    with lock:
        # Within the TTL we don't re-fetch; serve the last good reading while it
        # is still fresh, else nothing.
        if (now - cache["at"]) < ttl:
            return cache["good"] if (now - cache["good_at"]) < grace else None
        # Claim this refresh slot BEFORE releasing the lock so concurrent callers
        # on an expired cache don't stampede the source; they get the last good
        # reading (or None) until the fetch lands.
        cache["at"] = now
    # The blocking fetch runs OUTSIDE the lock — holding it across the fetch
    # would serialize every caller behind one slow probe.
    data = fetch()
    now2 = time.time()
    with lock:
        if data is not None:
            cache["good"] = data
            cache["good_at"] = now2
            return data
        # Fetch failed: reuse the last good reading if it hasn't gone stale.
        return cache["good"] if (now2 - cache["good_at"]) < grace else None
