"""Port of the Go ``session/git`` package's ``worktree_branch.go``.

Provides the multi-error combiner used by ``GitWorktree`` (exposed via
:class:`GitWorktreeBranchMixin`). The joined message header
(``multiple errors occurred:``) and the ``\\n  - `` per-error layout match the
Go source byte-for-byte.
"""

from __future__ import annotations

from typing import List, Optional

__all__ = [
    "GitWorktreeBranchMixin",
]


class GitWorktreeBranchMixin:
    """Error-combining method of ``GitWorktree`` (from ``worktree_branch.go``)."""

    def combine_errors(self, errs: List[BaseException]) -> Optional[BaseException]:
        """Combine multiple errors into a single error.

        Returns ``None`` for an empty list, the sole error for a single-element
        list, or a ``RuntimeError`` whose message is::

            multiple errors occurred:
              - <err1>
              - <err2>

        (no trailing newline), matching Go's ``combineErrors``.
        """
        if len(errs) == 0:
            return None
        if len(errs) == 1:
            return errs[0]

        err_msg = "multiple errors occurred:"
        for err in errs:
            err_msg += "\n  - " + str(err)
        return RuntimeError(err_msg)

    # Go-name alias
    combineErrors = combine_errors
