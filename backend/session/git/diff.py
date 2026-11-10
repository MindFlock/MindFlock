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

import contextlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Dict, Iterator, Optional, Tuple

from backend.session.git.util import _exit_error

__all__ = [
    "DiffStats",
    "parse_numstat",
    "parseNumstat",
    "GitWorktreeDiffMixin",
    "scratch_index_env",
    "scratch_index_env_or_none",
]


# --------------------------------------------------------------------------- #
# Read-only "untracked files count" diffs.
#
# ``git diff`` only sees an untracked file once it is in the index, so the
# classic trick is ``git add -N .`` (intent-to-add) before diffing. Run on the
# person's own checkout that WRITES their index: every new file turns from
# ``??`` into `` A``, a deleted tracked file is staged as ``D `` (``add -N``
# with a pathspec records removals too), and the probe races their own git
# commands for ``index.lock``. MindFlock polls this every few seconds, and
# several sessions can run in place in one shared checkout, so the diff paths
# below do the ``add -N`` against a private COPY of the index instead
# (``GIT_INDEX_FILE``). Same diff output, and the real index is never written.
# --------------------------------------------------------------------------- #
_SCRATCH_TIMEOUT = 60.0


@contextlib.contextmanager
def scratch_index_env(
    worktree_path: str, timeout: float = _SCRATCH_TIMEOUT
) -> Iterator[Dict[str, str]]:
    """Yield a ``subprocess`` env whose ``GIT_INDEX_FILE`` is a throwaway copy
    of ``worktree_path``'s index with untracked files intent-to-added.

    Run ``git diff …`` with ``env=`` this to get the classic ``add -N .``
    output (new files included) without touching the real index. The copy
    lives in a private temp dir removed on exit. Raises ``RuntimeError`` when
    the index cannot be located (not a repo, git trouble): falling back to an
    empty index would report every tracked file as changed. The ``add -N``
    itself is best-effort: if it fails, the diff simply leaves untracked files
    out, as the old in-place probe did.
    """
    env = dict(os.environ)
    env.pop("GIT_INDEX_FILE", None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    cp = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "--git-path", "index"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=timeout,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            "git command failed: {} ({})".format(
                cp.stdout.decode("utf-8", "replace"), _exit_error(cp.returncode)
            )
        )
    real = os.path.join(worktree_path, cp.stdout.decode("utf-8", "replace").strip())
    tmpdir = tempfile.mkdtemp(prefix="mindflock-index-")
    try:
        scratch = os.path.join(os.path.abspath(tmpdir), "index")
        try:
            shutil.copyfile(real, scratch)
        except FileNotFoundError:
            pass  # no index yet (fresh repo): an empty one is the truth
        env["GIT_INDEX_FILE"] = scratch
        subprocess.run(
            ["git", "-C", worktree_path, "add", "-N", "."],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=timeout,
        )
        yield env
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@contextlib.contextmanager
def scratch_index_env_or_none(
    worktree_path: str,
) -> Iterator[Optional[Dict[str, str]]]:
    """:func:`scratch_index_env`, or ``None`` when it cannot be set up (not a
    repo, git trouble), for callers that then run their read-only diffs
    plainly (untracked files left out) rather than fail. Only the setup is
    caught; an error inside the ``with`` body propagates as usual."""
    with contextlib.ExitStack() as stack:
        try:
            env: Optional[Dict[str, str]] = stack.enter_context(
                scratch_index_env(worktree_path)
            )
        except (RuntimeError, OSError, subprocess.SubprocessError):
            env = None
        yield env


def _scratch_git(worktree_path: str, *args: str) -> str:
    """``git -C <wt> <args…>`` against a scratch intent-to-add index; errors
    in ``run_git_command``'s Go format (``git command failed: <out> (<err>)``).
    """
    with scratch_index_env(worktree_path) as env:
        try:
            cp = subprocess.run(
                ["git", "-C", worktree_path, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                timeout=_SCRATCH_TIMEOUT,
            )
        except subprocess.TimeoutExpired as err:
            raise RuntimeError(
                "git command failed: {} (timed out after {:g}s)".format(
                    (err.output or b"").decode("utf-8", "replace"), _SCRATCH_TIMEOUT
                )
            ) from err
    out = cp.stdout.decode("utf-8", "replace")
    if cp.returncode != 0:
        raise RuntimeError(
            "git command failed: {} ({})".format(out, _exit_error(cp.returncode))
        )
    return out


# --------------------------------------------------------------------------- #
# Short-TTL diff cache.
#
# ``Diff()`` / ``DiffNumstat()`` each run an untracked-file listing plus a
# full ``git diff HEAD`` — and the web UI's per-tick pollers call them once per
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


def _is_peer_share(path: str) -> bool:
    """True for a peer link's shared folder (lazy: worktree_git imports us)."""
    from backend.session.git.worktree_git import _is_peer_share as check

    return check(path)


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

        Runs ``git -C <wt> --no-pager diff HEAD`` and counts added/removed
        lines. Untracked files are included: when there are any, the diff runs
        against a scratch intent-to-add index (:func:`scratch_index_env`), so
        the person's own index is never written. On any git failure, returns
        a ``DiffStats`` carrying the error and no content.

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
        try:
            content = self._diff_with_untracked("--no-pager", "diff", "HEAD")
        except Exception as err:  # noqa: BLE001 - propagate like Go's stats.Error
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

    def _diff_with_untracked(self, *args: str) -> str:
        """``git <args>`` (a ``diff … HEAD``) with untracked files counted,
        never writing the worktree's index.

        No untracked files (the common idle case): a plain diff through
        ``run_git_command``. Otherwise the diff runs against a scratch
        intent-to-add index. Never in a peer shared folder: intent-to-add
        there could let an agent-made nested repo into the trusted index
        (see worktree_git), so a share's diff is tracked files only.
        """
        wt = self.worktreePath
        if not _is_peer_share(wt):
            untracked = self.run_git_command(
                wt, "ls-files", "-z", "--others", "--exclude-standard"
            )
            if untracked.strip("\0\n"):
                return _scratch_git(wt, *args)
        return self.run_git_command(wt, *args)

    def DiffNumstat(self) -> DiffStats:
        """Return only the added/removed line counts without loading the full
        diff content into memory.

        Runs ``git -C <wt> --no-pager diff --numstat HEAD`` (untracked files
        included, read-only — see :meth:`Diff`) and sums the columns.
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
        try:
            out = self._diff_with_untracked("--no-pager", "diff", "--numstat", "HEAD")
        except Exception as err:  # noqa: BLE001
            stats.Error = err
            return stats

        stats.Added, stats.Removed = parse_numstat(out)
        return stats


# Go-name alias.
parseNumstat = parse_numstat
