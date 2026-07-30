"""Port of the Go ``session/git`` package's ``worktree_git.go``.

Holds the git-and-gh-driven methods of ``GitWorktree`` (exposed via
:class:`GitWorktreeGitMixin`), plus the free functions ``FetchBranches`` /
``SearchBranches`` and the constant ``MaxBranchSearchResults``.

Command argv and error strings are byte-for-byte identical to the Go source,
with one deliberate divergence: Go pushed through ``gh repo sync``, which made
the GitHub CLI mandatory for a push. Pushing here is a **bare**
``git push -u origin <branch>`` with ``cwd`` set to the worktree path (no
``-C``), so it works over whatever remote the user configured — SSH or HTTPS,
gh or no gh.
"""

from __future__ import annotations

import os
import subprocess
import webbrowser
from typing import List

from backend import log
from backend.session.git.remote_url import branch_url, is_local_path
from backend.session.git.util import _exit_error, _trim_prefix, gh_available

__all__ = [
    "MaxBranchSearchResults",
    "FetchBranches",
    "fetch_branches",
    "SearchBranches",
    "search_branches",
    "GitWorktreeGitMixin",
]

# MaxBranchSearchResults is the maximum number of branches returned by
# SearchBranches.
MaxBranchSearchResults: int = 50

# Default subprocess budgets (seconds): local git commands vs. network
# operations (push/fetch/sync), which can legitimately take minutes.
_GIT_TIMEOUT: float = 60.0
_NET_TIMEOUT: float = 600.0


def fetch_branches(repo_path: str) -> None:
    """Fetch and prune remote-tracking branches (best-effort).

    Runs ``git -C <repoPath> fetch --prune`` and ignores any failure so the
    caller never breaks when offline (Go: ``_ = cmd.Run()``).
    """
    try:
        subprocess.run(
            ["git", "-C", repo_path, "fetch", "--prune"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_NET_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # Best-effort fetch: a hung network is treated like being offline.
        pass


def search_branches(repo_path: str, filter: str) -> List[str]:
    """Search for branches whose name contains ``filter`` (case-insensitive),
    ordered most-recently-updated first; at most ``MaxBranchSearchResults``.

    If ``filter`` is empty, returns all branches up to the limit. Strips the
    ``origin/`` prefix before de-duplicating and keeps the first occurrence.
    Lines containing ``HEAD`` are skipped.

    Raises ``RuntimeError("failed to list branches: <output> (<err>)")`` on a
    git failure (matches Go's combined-output error format).
    """
    try:
        cmd = subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "branch",
                "-a",
                "--sort=-committerdate",
                "--format=%(refname:short)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # CombinedOutput()
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(
            "failed to list branches: {} (timed out after {:g}s)".format(
                _decode(err.output or b""), _GIT_TIMEOUT
            )
        ) from err
    output = cmd.stdout
    if cmd.returncode != 0:
        raise RuntimeError(
            "failed to list branches: {} ({})".format(
                _decode(output), _exit_error(cmd.returncode)
            )
        )

    seen = {}
    branches = []
    lower = filter.lower()
    for line in _decode(output).strip().split("\n"):
        line = line.strip()
        if line == "" or "HEAD" in line:
            continue
        name = _trim_prefix(line, "origin/")
        if seen.get(name):
            continue
        seen[name] = True
        if filter != "" and lower not in name.lower():
            continue
        branches.append(name)
        if len(branches) >= MaxBranchSearchResults:
            break
    return branches


class GitWorktreeGitMixin:
    """Git/gh-driven methods of ``GitWorktree`` (from Go's ``worktree_git.go``).

    Requires the host class to provide ``repoPath``, ``worktreePath`` and
    ``branchName`` attributes.
    """

    # --- runGitCommand ----------------------------------------------------
    def run_git_command(
        self, path: str, *args: str, timeout: float = _GIT_TIMEOUT
    ) -> str:
        """Execute ``git -C <path> <args...>`` and return combined output.

        On a non-zero exit raises ``RuntimeError`` with Go's exact format
        ``git command failed: <output> (<err>)``. Network operations should
        pass a larger ``timeout`` (e.g. ``_NET_TIMEOUT``); on expiry the child
        is killed and a ``RuntimeError`` in the same format is raised.
        """
        base_args = ["-C", path]
        try:
            cmd = subprocess.run(
                ["git", *base_args, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # CombinedOutput()
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as err:
            raise RuntimeError(
                "git command failed: {} (timed out after {:g}s)".format(
                    _decode(err.output or b""), timeout
                )
            ) from err
        output = cmd.stdout
        if cmd.returncode != 0:
            raise RuntimeError(
                "git command failed: {} ({})".format(
                    _decode(output), _exit_error(cmd.returncode)
                )
            )
        return _decode(output)

    # --- PushChanges ------------------------------------------------------
    def PushChanges(self, commit_message: str, open: bool) -> None:
        """Commit and push changes in the worktree to the remote branch.

        Steps: commit dirty changes, then a bare ``git push -u origin
        <branch>``. If ``open`` is set, open the branch URL (failures there are
        logged only).

        The push is plain git on purpose. It goes over whatever remote the user
        configured — SSH or HTTPS — and needs no GitHub CLI and no token, so a
        contributor whose git config uses SSH can push exactly as she always
        does. (Go drove this through ``gh repo sync``, which both required gh
        and was the wrong command: ``repo sync`` updates a fork from its
        upstream, it does not publish a branch.)
        """
        # Check if there are any changes to commit.
        try:
            is_dirty = self.IsDirty()
        except Exception as err:  # noqa: BLE001
            raise RuntimeError("failed to check for changes: {}".format(err)) from err

        if is_dirty:
            # Stage all changes.
            try:
                self.run_git_command(self.worktreePath, "add", ".")
            except Exception as err:  # noqa: BLE001
                if log.ErrorLog is not None:
                    log.ErrorLog.Print(err)
                raise RuntimeError("failed to stage changes: {}".format(err)) from err

            # Create commit.
            try:
                self.run_git_command(
                    self.worktreePath,
                    "commit",
                    "-m",
                    commit_message,
                    "--no-verify",
                )
            except Exception as err:  # noqa: BLE001
                if log.ErrorLog is not None:
                    log.ErrorLog.Print(err)
                raise RuntimeError("failed to commit changes: {}".format(err)) from err

        # Publish the branch. `-u` sets upstream tracking so the branch exists
        # on the remote and later pushes need no arguments.
        # NOTE: bare `git push` (no -C); cwd is the worktree path.
        try:
            git_push_cmd = subprocess.run(
                ["git", "push", "-u", "origin", self.branchName],
                cwd=self.worktreePath,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # CombinedOutput()
                timeout=_NET_TIMEOUT,
            )
        except subprocess.TimeoutExpired as err:
            push_err = "timed out after {:g}s".format(_NET_TIMEOUT)
            if log.ErrorLog is not None:
                log.ErrorLog.Print(push_err)
            raise RuntimeError(
                "failed to push branch: {} ({})".format(
                    _decode(err.output or b""), push_err
                )
            ) from err
        if git_push_cmd.returncode != 0:
            push_err = _exit_error(git_push_cmd.returncode)
            if log.ErrorLog is not None:
                log.ErrorLog.Print(push_err)
            raise RuntimeError(
                "failed to push branch: {} ({})".format(
                    _decode(git_push_cmd.stdout), push_err
                )
            )

        # Open the branch in the browser.
        if open:
            try:
                self.OpenBranchURL()
            except Exception as err:  # noqa: BLE001
                # Just log the error but don't fail the push operation.
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("failed to open branch URL: %v", err)

    # --- CommitChanges ----------------------------------------------------
    def CommitChanges(self, commit_message: str) -> None:
        """Commit changes locally without pushing to remote."""
        # Check if there are any changes to commit.
        try:
            is_dirty = self.IsDirty()
        except Exception as err:  # noqa: BLE001
            raise RuntimeError("failed to check for changes: {}".format(err)) from err

        if is_dirty:
            # Stage all changes.
            try:
                self.run_git_command(self.worktreePath, "add", ".")
            except Exception as err:  # noqa: BLE001
                if log.ErrorLog is not None:
                    log.ErrorLog.Print(err)
                raise RuntimeError("failed to stage changes: {}".format(err)) from err

            # Create commit (local only).
            try:
                self.run_git_command(
                    self.worktreePath,
                    "commit",
                    "-m",
                    commit_message,
                    "--no-verify",
                )
            except Exception as err:  # noqa: BLE001
                if log.ErrorLog is not None:
                    log.ErrorLog.Print(err)
                raise RuntimeError("failed to commit changes: {}".format(err)) from err

    # --- IsDirty ----------------------------------------------------------
    def IsDirty(self) -> bool:
        """Return whether the worktree has uncommitted changes.

        Dirty iff ``git -C <wt> status --porcelain`` produces non-empty output.
        """
        try:
            output = self.run_git_command(self.worktreePath, "status", "--porcelain")
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(
                "failed to check worktree status: {}".format(err)
            ) from err
        return len(output) > 0

    # --- IsValidWorktree --------------------------------------------------
    def IsValidWorktree(self) -> bool:
        """Return whether the worktree path exists and contains a ``.git`` entry.

        Returns ``False`` if the worktree is orphaned (path or ``.git``
        missing). Raises ``RuntimeError`` for other stat errors with Go's exact
        messages (``failed to stat worktree path``/``... .git``).
        """
        try:
            os.stat(self.worktreePath)
        except FileNotFoundError:
            return False
        except OSError as err:
            raise RuntimeError("failed to stat worktree path: {}".format(err)) from err

        try:
            os.stat(os.path.join(self.worktreePath, ".git"))
        except FileNotFoundError:
            return False
        except OSError as err:
            raise RuntimeError("failed to stat worktree .git: {}".format(err)) from err

        return True

    # --- IsBranchCheckedOut -----------------------------------------------
    def IsBranchCheckedOut(self) -> bool:
        """Return whether this instance's branch is currently checked out.

        Compares ``git -C <repo> branch --show-current`` (trimmed) to the
        worktree's branch name.
        """
        try:
            output = self.run_git_command(self.repoPath, "branch", "--show-current")
        except Exception as err:  # noqa: BLE001
            raise RuntimeError("failed to get current branch: {}".format(err)) from err
        return output.strip() == self.branchName

    # --- OpenBranchURL ----------------------------------------------------
    def OpenBranchURL(self) -> None:
        """Open the branch's page on the forge in the default browser.

        ``gh browse --branch <branch>`` is preferred when gh is installed and
        authenticated: it knows about forks and the user's gh host config. When
        gh is missing, logged out, hung, or simply fails, this falls through to
        deriving the URL from ``origin`` itself and handing it to the stdlib
        browser opener — the same page, without the CLI.

        Raises ``RuntimeError("failed to open branch URL: <reason>")`` only when
        no URL can be produced at all (e.g. ``origin`` is a local clone path,
        which has no branch page) or no browser could be launched.
        """
        # Preferred path: let gh do it when it is actually usable.
        if gh_available():
            try:
                cmd = subprocess.run(
                    ["gh", "browse", "--branch", self.branchName],
                    cwd=self.worktreePath,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_GIT_TIMEOUT,
                )
                if cmd.returncode == 0:
                    return
            except (subprocess.TimeoutExpired, OSError):
                # A hung or unusable gh is not a failure to report: the plain
                # URL below reaches the same page.
                pass

        # Fallback: whatever remote the user configured already names the repo.
        try:
            origin = self.run_git_command(
                self.worktreePath, "remote", "get-url", "origin"
            ).strip()
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(
                "failed to open branch URL: could not read remote 'origin': {}".format(
                    err
                )
            ) from err

        url = branch_url(origin, self.branchName)
        if url is None:
            if is_local_path(origin):
                reason = (
                    "remote 'origin' is a local path ({}), which has no branch "
                    "page to open".format(origin)
                )
            else:
                reason = "remote 'origin' ({}) is not a recognised forge URL".format(
                    origin
                )
            raise RuntimeError("failed to open branch URL: {}".format(reason))

        if not webbrowser.open(url):
            raise RuntimeError(
                "failed to open branch URL: no browser available for {}".format(url)
            )

    # snake_case aliases
    push_changes = PushChanges
    commit_changes = CommitChanges
    is_dirty = IsDirty
    is_valid_worktree = IsValidWorktree
    is_branch_checked_out = IsBranchCheckedOut
    open_branch_url = OpenBranchURL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _decode(b: bytes) -> str:
    """Decode subprocess output bytes the way Go's ``string([]byte)`` would."""
    if b is None:
        return ""
    return b.decode("utf-8", "replace")


# Go-name aliases.
FetchBranches = fetch_branches
SearchBranches = search_branches
