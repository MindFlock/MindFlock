"""Port of the Go ``session/git`` package's ``diff.go``.

Defines :class:`DiffStats` (the diff summary value object), the
``parse_numstat`` helper, and the ``Diff`` / ``DiffNumstat`` worktree methods
(exposed via :class:`GitWorktreeDiffMixin`, mixed into ``GitWorktree``).

Diff line counting and numstat parsing match the Go source byte-for-byte:
  * count lines starting with ``+`` (but not ``+++``) and ``-`` (but not ``---``);
  * numstat lines are split with ``SplitN(line, "\\t", 3)``; binary ``-\\t-``
    entries are silently skipped.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

__all__ = [
    "DiffStats",
    "parse_numstat",
    "parseNumstat",
    "GitWorktreeDiffMixin",
]


# --------------------------------------------------------------------------- #
# Short-TTL diff cache.
#
# ``Diff()`` / ``DiffNumstat()`` each run ``git add -N .`` plus a full
# ``git diff HEAD`` — and the web UI's per-tick pollers call them once per
# instance per ~4s tick, so idle sessions re-diff forever. Results are
# memoized ~_DIFF_TTL seconds per worktree path (full diff and numstat cached
# INDEPENDENTLY: they answer different questions and the numstat one is much
# cheaper to recompute). A just-edited / just-committed worktree therefore
# shows fresh data within one TTL — well under the UI's own poll cadence.
# Written from FastAPI worker threads and asyncio.to_thread workers -> lock.
# --------------------------------------------------------------------------- #
_DIFF_TTL = 2.5
_DIFF_CACHE: Dict[Tuple[str, str], Tuple[float, "DiffStats"]] = {}
_DIFF_CACHE_LOCK = threading.Lock()


def _diff_cache_get(key: Tuple[str, str]) -> Optional["DiffStats"]:
    now = time.monotonic()
    with _DIFF_CACHE_LOCK:
        hit = _DIFF_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    return None


def _diff_cache_put(key: Tuple[str, str], stats: "DiffStats") -> None:
    with _DIFF_CACHE_LOCK:
        _DIFF_CACHE[key] = (time.monotonic() + _DIFF_TTL, stats)


def _copy_stats(stats: "DiffStats") -> "DiffStats":
    """A fresh DiffStats so callers can't mutate the cached entry."""
    return DiffStats(
        content=stats.Content,
        added=stats.Added,
        removed=stats.Removed,
        error=stats.Error,
    )


class DiffStats:
    """Statistics about the changes in a diff.

    Mirrors Go's ``DiffStats`` struct:
      * ``content`` - the full diff content (str)
      * ``added``   - number of added lines (int)
      * ``removed`` - number of removed lines (int)
      * ``error``   - any error that occurred during diff computation. This
        allows propagating setup errors (like a missing base commit) without
        breaking the flow.
    """

    def __init__(
        self,
        content: str = "",
        added: int = 0,
        removed: int = 0,
        error: Optional[BaseException] = None,
    ) -> None:
        self.Content: str = content
        self.Added: int = added
        self.Removed: int = removed
        self.Error: Optional[BaseException] = error


def parse_numstat(out: str) -> Tuple[int, int]:
    """Sum the added/removed columns from ``git diff --numstat`` output.

    Each line is formatted as ``<added>\\t<removed>\\t<path>``. Binary files
    report ``-\\t-\\t<path>`` and are ignored for line totals. Returns
    ``(added, removed)``.

    The path is split with ``SplitN(line, "\\t", 3)`` so paths containing tabs
    are preserved (and never affect the integer parse of the first two fields).
    """
    added = 0
    removed = 0
    for line in out.split("\n"):
        if line == "":
            continue
        fields = line.split("\t", 2)  # SplitN(line, "\t", 3) -> max 3 parts
        if len(fields) < 2:
            continue
        try:
            a = _atoi(fields[0])
            r = _atoi(fields[1])
        except ValueError:
            # Either column failed to parse (e.g. binary "-"): skip the line.
            continue
        added += a
        removed += r
    return added, removed


def _atoi(s: str) -> int:
    """Mirror Go's ``strconv.Atoi``: parse a base-10 integer or raise.

    Go's ``Atoi`` accepts an optional leading sign and rejects surrounding
    whitespace or any trailing characters, raising for anything else. Python's
    ``int(s, 10)`` matches closely enough for numstat columns, but it tolerates
    surrounding whitespace, so we guard against that explicitly.
    """
    if s == "" or s.strip() != s:
        raise ValueError("invalid integer: {!r}".format(s))
    return int(s, 10)


class GitWorktreeDiffMixin:
    """Diff-related methods of ``GitWorktree`` (from Go's ``diff.go``).

    Requires the host class to provide ``worktreePath`` (attribute),
    ``run_git_command(path, *args)`` and ``GetBaseCommitSHA()``.
    """

    def Diff(self) -> DiffStats:
        """Return the git diff of uncommitted changes (working tree vs HEAD).

        Stages untracked files with ``git -C <wt> add -N .`` (intent-to-add so
        they appear in the diff), runs ``git -C <wt> --no-pager diff HEAD``,
        then counts added/removed lines. On any git failure, returns a
        ``DiffStats`` carrying the error and no content.

        Memoized ~``_DIFF_TTL`` seconds per worktree path (see the cache note
        at the top of this module); results may be that slightly stale.
        """
        key = (self.worktreePath, "full")
        if self.worktreePath:
            cached = _diff_cache_get(key)
            if cached is not None:
                return _copy_stats(cached)
        stats = self._diff_uncached()
        if self.worktreePath:
            _diff_cache_put(key, _copy_stats(stats))
        return stats

    def _diff_uncached(self) -> DiffStats:
        stats = DiffStats()

        # -N stages untracked files (intent to add), including them in the diff.
        try:
            self.run_git_command(self.worktreePath, "add", "-N", ".")
        except Exception as err:  # noqa: BLE001 - propagate like Go's stats.Error
            stats.Error = err
            return stats

        try:
            content = self.run_git_command(
                self.worktreePath, "--no-pager", "diff", "HEAD"
            )
        except Exception as err:  # noqa: BLE001
            stats.Error = err
            return stats

        lines = content.split("\n")
        for line in lines:
            if line.startswith("+") and not line.startswith("+++"):
                stats.Added += 1
            elif line.startswith("-") and not line.startswith("---"):
                stats.Removed += 1
        stats.Content = content

        return stats

    def DiffNumstat(self) -> DiffStats:
        """Return only the added/removed line counts without loading the full
        diff content into memory.

        Stages untracked files (``add -N .``), runs
        ``git -C <wt> --no-pager diff --numstat HEAD``, and sums the columns.
        Use this when only the summary counts are needed (e.g. for unselected
        instances in the list).

        Memoized ~``_DIFF_TTL`` seconds per worktree path, independently of
        :meth:`Diff` (see the cache note at the top of this module).
        """
        key = (self.worktreePath, "numstat")
        if self.worktreePath:
            cached = _diff_cache_get(key)
            if cached is not None:
                return _copy_stats(cached)
        stats = self._diff_numstat_uncached()
        if self.worktreePath:
            _diff_cache_put(key, _copy_stats(stats))
        return stats

    def _diff_numstat_uncached(self) -> DiffStats:
        stats = DiffStats()

        # -N stages untracked files (intent to add), including them in the diff.
        try:
            self.run_git_command(self.worktreePath, "add", "-N", ".")
        except Exception as err:  # noqa: BLE001
            stats.Error = err
            return stats

        try:
            out = self.run_git_command(
                self.worktreePath,
                "--no-pager",
                "diff",
                "--numstat",
                "HEAD",
            )
        except Exception as err:  # noqa: BLE001
            stats.Error = err
            return stats

        stats.Added, stats.Removed = parse_numstat(out)
        return stats


# Go-name alias.
parseNumstat = parse_numstat
