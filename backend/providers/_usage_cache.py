"""Shared live-usage cache primitives for the provider ``usage_api`` modules.

:func:`serve` — process-local, for LOCAL probes: codex_usage_api and
antigravity_usage_api each expose a ``live_usage()`` with the identical
concurrency shape: throttle refreshes to a
TTL, serve the last known-good reading through transient failures for a grace
window, and — critically — run the blocking fetch OUTSIDE the module lock so
one slow probe can't serialize every ``/api/usage`` caller. That invariant
lives here once, so a fix to it can't miss one of the three copies.

The per-module cache dict, lock, TTL, grace and fetch are passed in (resolved
as module globals at the call site) so the modules' monkeypatch-based tests —
which reassign ``api._cache`` and patch ``api._fetch`` — keep working unchanged.

:class:`SharedReading` — one reading per MACHINE, for a rate-limited REMOTE
meter (claude_usage_api: Anthropic's OAuth usage endpoint): a shared file, a
cross-process fetch lock and a 429 backoff every process honours.
"""

from __future__ import annotations

import json as _json
import logging as _logging
import os as _os
import random as _random
import threading as _threading
import time
from typing import Callable, Optional

try:  # POSIX; a platform without flock still gets the file + backoff
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — Windows
    _fcntl = None  # type: ignore[assignment]


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


# --------------------------------------------------------------------------- #
# One reading per MACHINE: the shared-file cache
# --------------------------------------------------------------------------- #
# :func:`serve` above keeps its cache in process memory, which is right for a
# local probe (Codex's on-disk snapshot, Antigravity's language server) but
# wrong for a rate-limited REMOTE meter: every MindFlock process on the machine
# (the live server, a dev server, the ticket pipeline child) polled Anthropic's
# usage endpoint with the same account on its own clock, with no backoff, and
# kept the account answering 429 indefinitely. :class:`SharedReading` keeps the
# reading in ONE file per machine that every process reads first; only the
# process that finds it stale fetches, under a non-blocking ``flock``, and a
# 429/5xx records an exponential backoff in the same file so every process
# honours it. Through a backoff the last good reading keeps being served, marked
# stale with its age, rather than dropping to a transcript estimate.

_log = _logging.getLogger(__name__)

#: Statuses that mean "you are asking too often / we are struggling": back off.
_BACKOFF_STATUSES = (429,)


def run_dir() -> Optional[str]:
    """``$MINDFLOCK_RUN_DIR``, else ``<config dir>/run`` — the run dir the MCP
    run files and slots already use (tests point it at a tmp dir). ``None``
    when no config dir resolves (no ``$HOME``; under pytest, the real one)."""
    env = (_os.environ.get("MINDFLOCK_RUN_DIR") or "").strip()
    if env:
        return env
    try:
        from backend.config.config import GetConfigDir

        return _os.path.join(GetConfigDir(), "run")
    except Exception:  # noqa: BLE001 — no home: process-local only
        return None


def backoff_delay(failures: int, base: float, cap: float, rand=None) -> float:
    """Seconds to wait after the ``failures``-th consecutive throttled fetch:
    ``base`` doubling per failure up to ``cap``, with ±20 % jitter (so the
    processes and devices sharing an account do not retry in lock-step)."""
    n = max(1, int(failures))
    delay = min(cap, base * (2 ** (n - 1)))
    r = (rand or _random.random)()
    return max(1.0, min(cap, delay * (0.8 + 0.4 * r)))


class SharedReading:
    """A remote meter's reading, shared by every process on the machine.

    ``fetch()`` returns ``(reading | None, status | None, retry_after | None)``:
    the normalized reading on success, else the HTTP status (None for a
    network/credential failure) and the server's ``Retry-After`` in seconds.

    The file (``<run dir>/<name>.json``, written atomically) holds::

        {"reading": dict|None, "fetched_at": epoch,   # the last GOOD reading
         "attempt_at": epoch, "backoff_until": epoch, "failures": int,
         "last_status": int|None, "plan": dict}       # see ``plan_update``

    ``plan_update(plan, reading, now) -> plan`` (optional) folds a good
    reading into the remembered plan shape, which survives later failures.

    When the run dir is unavailable everything runs from process memory with
    the same TTL/backoff rules.
    """

    def __init__(
        self,
        name: str,
        fetch: Callable[[], tuple],
        *,
        ttl: float = 60.0,
        stale_after: float = 150.0,
        stale_max: float = 6 * 3600.0,
        backoff_base: float = 120.0,
        backoff_cap: float = 900.0,
        retry_after_cap: float = 3600.0,
        plan_update: Optional[Callable[[dict, dict, float], dict]] = None,
        path: Optional[str] = None,
    ) -> None:
        self.name = name
        self.fetch = fetch
        self.ttl = ttl
        self.stale_after = stale_after
        self.stale_max = stale_max
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.retry_after_cap = retry_after_cap
        self.plan_update = plan_update
        self._path = path
        self._lock = _threading.Lock()
        self._inflight = False
        self._mem: dict = {}

    # --- storage ---------------------------------------------------------- #
    def path(self) -> Optional[str]:
        if self._path:
            return self._path
        d = run_dir()
        if not d:
            return None
        try:
            from backend.config.home_guard import guard

            return guard(_os.path.join(d, self.name + ".json"), "usage cache")
        except Exception:  # noqa: BLE001 — refused (pytest real home): memory
            return None

    def load(self) -> dict:
        p = self.path()
        if not p:
            return dict(self._mem)
        try:
            with open(p, "r", encoding="utf-8") as f:
                doc = _json.load(f)
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _save(self, st: dict) -> None:
        p = self.path()
        if not p:
            self._mem = dict(st)
            return
        try:
            from backend.fsutil import atomic_write_json

            _os.makedirs(_os.path.dirname(p), exist_ok=True)
            atomic_write_json(p, st, indent=None, prefix="." + self.name + ".")
        except OSError as e:
            _log.debug("usage cache %s not written (%s)", self.name, e)
            self._mem = dict(st)

    def _try_flock(self):
        """An open fd holding the exclusive fetch lock, ``False`` when another
        process holds it, or ``None`` when there is nothing to lock with."""
        p = self.path()
        if not p or _fcntl is None:
            return None
        try:
            _os.makedirs(_os.path.dirname(p), exist_ok=True)
            fd = _os.open(p + ".lock", _os.O_RDWR | _os.O_CREAT, 0o600)
        except OSError:
            return None
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            _os.close(fd)
            return False
        return fd

    @staticmethod
    def _unlock(fd) -> None:
        if fd is None or fd is False:
            return
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            _os.close(fd)

    # --- policy ----------------------------------------------------------- #
    def _f(self, st: dict, key: str) -> float:
        try:
            return float(st.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _due(self, st: dict, now: float) -> bool:
        """Whether ``st`` calls for a fetch now: no fresh reading, no backoff
        in force, and no attempt (by any process) within the TTL."""
        if st.get("reading") and now - self._f(st, "fetched_at") < self.ttl:
            return False
        if now < self._f(st, "backoff_until"):
            return False
        return now - self._f(st, "attempt_at") >= self.ttl

    def view(
        self, st: dict, now: float, max_age: Optional[float] = None
    ) -> Optional[dict]:
        """The reading to serve from ``st`` (a copy carrying ``fetched_at``,
        ``stale`` and ``backoff_until``), or None when there has never been a
        good reading or the last one is older than ``max_age`` (default
        ``stale_max``)."""
        rd = st.get("reading")
        if not isinstance(rd, dict) or not rd:
            return None
        at = self._f(st, "fetched_at")
        age = now - at
        if age > (self.stale_max if max_age is None else min(max_age, self.stale_max)):
            return None
        out = dict(rd)
        out["fetched_at"] = at
        out["stale"] = age > self.stale_after
        bo = self._f(st, "backoff_until")
        out["backoff_until"] = bo if bo > now else 0.0
        return out

    # --- the read --------------------------------------------------------- #
    def get(self, max_age: Optional[float] = None) -> Optional[dict]:
        """The machine's reading, fetching first only when it is due (and only
        in the one process that wins the lock). ``max_age`` narrows how old a
        reading may be served (a caller DECIDING on it wants a recent one).
        Never raises."""
        now = time.time()
        with self._lock:
            st = self.load()
            if not self._due(st, now) or self._inflight:
                return self.view(st, now, max_age)
            self._inflight = True
        fd = None
        try:
            fd = self._try_flock()
            if fd is False:  # another process is fetching right now
                return self.view(self.load(), now, max_age)
            st = self.load()  # re-check under the lock: it may just have landed
            if not self._due(st, time.time()):
                return self.view(st, time.time(), max_age)
            st["attempt_at"] = time.time()
            self._save(st)
            try:
                reading, status, retry_after = self.fetch()
            except Exception:  # noqa: BLE001 — a fetch never breaks a caller
                reading, status, retry_after = None, None, None
            now2 = time.time()
            st = self.load() or st
            st["attempt_at"] = now2
            st["last_status"] = status
            if reading:
                st["reading"] = reading
                st["fetched_at"] = now2
                st["failures"] = 0
                st["backoff_until"] = 0.0
                if self.plan_update is not None:
                    try:
                        plan = st.get("plan")
                        st["plan"] = self.plan_update(
                            plan if isinstance(plan, dict) else {}, reading, now2
                        )
                    except Exception:  # noqa: BLE001 — shape memory is enrichment
                        pass
            elif status in _BACKOFF_STATUSES or (status or 0) >= 500:
                failures = int(self._f(st, "failures")) + 1
                st["failures"] = failures
                try:
                    ra = float(retry_after or 0.0)
                except (TypeError, ValueError):
                    ra = 0.0
                if ra > 0:
                    delay = min(ra, self.retry_after_cap)
                else:
                    delay = backoff_delay(failures, self.backoff_base, self.backoff_cap)
                st["backoff_until"] = now2 + delay
                _log.debug(
                    "%s: HTTP %s, backing off %.0fs (failure %d)",
                    self.name,
                    status,
                    delay,
                    failures,
                )
            self._save(st)
            return self.view(st, now2, max_age)
        finally:
            self._unlock(fd)
            with self._lock:
                self._inflight = False

    def plan(self) -> dict:
        """The remembered plan shape (``{}`` when none yet). File read only —
        never fetches."""
        p = self.load().get("plan")
        return p if isinstance(p, dict) else {}
