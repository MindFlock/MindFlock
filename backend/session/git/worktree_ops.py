"""Port of the Go ``session/git`` package's ``worktree_ops.go``.

Holds the worktree lifecycle methods of ``GitWorktree`` (exposed via
:class:`GitWorktreeOpsMixin`): ``Setup`` and its ``setup_from_existing_branch``
/ ``setup_new_worktree`` helpers, ``Cleanup``, ``Remove`` and ``Prune`` — plus
the free function ``CleanupWorktrees``.

Command argv, error strings, HEAD-detection substrings and the
``branch -D ... not found`` skip behavior match the Go source byte-for-byte.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from backend import log
from backend.session.git.util import _exit_error, _trim_prefix

__all__ = [
    "GitWorktreeOpsMixin",
    "CleanupWorktrees",
    "cleanup_worktrees",
]


def _safe_rmtree(path: str, repo_path: str) -> None:
    """``shutil.rmtree(path)`` with a guard against catastrophic targets.

    The worktree path is normally MindFlock-owned (``~/.mindflock/worktrees/…``
    or a provisioned workspace dir), but it is ultimately derived state — so
    refuse to delete the filesystem root, the user's home directory, the
    source repo itself, or any ancestor of the source repo. A refused delete
    is silently skipped (``git worktree add`` will then fail with its own
    clear error rather than us destroying user data)."""
    target = os.path.realpath(path)
    repo = os.path.realpath(repo_path) if repo_path else ""
    home = os.path.realpath(os.path.expanduser("~"))
    if target in (os.sep, home):
        return
    if repo and (target == repo or repo.startswith(target + os.sep)):
        return
    shutil.rmtree(target, ignore_errors=True)


class GitWorktreeOpsMixin:
    """Worktree lifecycle methods of ``GitWorktree`` (from ``worktree_ops.go``).

    Requires the host class to provide ``repoPath``, ``worktreePath``,
    ``branchName``, ``isExistingBranch``, ``baseCommitSHA`` attributes,
    ``run_git_command(path, *args)``, ``combine_errors(errs)`` and the (lazily
    imported) ``get_worktree_directory()`` helper.
    """

    def _rollback_partial_worktree(self, *, delete_branch: bool) -> None:
        """Best-effort rollback after an interrupted/failed ``worktree add``.

        Removes the (possibly half-created) worktree registration and
        directory, optionally deletes the just-created branch, and prunes —
        so a Ctrl-C mid-creation leaves the source repo exactly as it was
        and the same session title can be retried immediately. Never raises.
        """
        try:
            self.run_git_command(
                self.repoPath, "worktree", "remove", "-f", self.worktreePath
            )
        except Exception:  # noqa: BLE001
            pass
        _safe_rmtree(self.worktreePath, self.repoPath)
        if delete_branch:
            try:
                self.run_git_command(self.repoPath, "branch", "-D", self.branchName)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.run_git_command(self.repoPath, "worktree", "prune")
        except Exception:  # noqa: BLE001
            pass

    # --- Setup ------------------------------------------------------------
    def Setup(self) -> None:
        """Create a new worktree for the session.

        Ensures the worktrees directory exists, then either sets up from an
        existing branch (if ``isExistingBranch`` is set, or a local
        ``refs/heads/<branch>`` exists) or creates a brand-new worktree from
        HEAD.
        """
        from backend.session.git.worktree import get_worktree_directory

        # Ensure worktrees directory exists early.
        try:
            worktrees_dir = get_worktree_directory()
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(
                "failed to get worktree directory: {}".format(err)
            ) from err

        os.makedirs(worktrees_dir, mode=0o755, exist_ok=True)

        # If this worktree uses a pre-existing branch, always set up from that
        # branch (it may exist locally or only on the remote).
        if self.isExistingBranch:
            self.setup_from_existing_branch()
            self._enable_untracked_cache()
            return None

        # Check if branch exists using git CLI (much faster than go-git).
        try:
            self.run_git_command(
                self.repoPath,
                "show-ref",
                "--verify",
                "refs/heads/{}".format(self.branchName),
            )
            branch_exists = True
        except Exception:  # noqa: BLE001
            branch_exists = False

        if branch_exists:
            self.setup_from_existing_branch()
        else:
            self.setup_new_worktree()
        self._enable_untracked_cache()
        return None

    def _enable_untracked_cache(self) -> None:
        """Best-effort perf knob after a successful worktree add (a Python-side
        addition — not in the Go source): ``core.untrackedCache=true`` caches
        untracked-dir scans by directory mtime, speeding up the ``git status``
        / ``add -N`` calls the diff-stat probe runs against every session
        every ~10s. Repo-level config (worktrees share it), pure performance
        setting, never raises. ``core.fsmonitor`` would help more but git's
        builtin daemon does not support Linux (as of git 2.43)."""
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    self.worktreePath,
                    "config",
                    "core.untrackedCache",
                    "true",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except Exception:  # noqa: BLE001
            pass

    # --- setupFromExistingBranch ------------------------------------------
    def setup_from_existing_branch(self) -> None:
        """Create a worktree from an existing branch (local or remote)."""
        # Directory already created in Setup(), skip duplicate creation.

        # Clean up any existing worktree first (ignore error if absent).
        try:
            self.run_git_command(
                self.repoPath, "worktree", "remove", "-f", self.worktreePath
            )
        except Exception:  # noqa: BLE001
            pass
        # If the directory is still there (orphaned, not registered with git),
        # drop it so `git worktree add` won't fail.
        _safe_rmtree(self.worktreePath, self.repoPath)

        # Check if the local branch exists.
        try:
            self.run_git_command(
                self.repoPath,
                "show-ref",
                "--verify",
                "refs/heads/{}".format(self.branchName),
            )
            local_ok = True
        except Exception:  # noqa: BLE001
            local_ok = False

        if not local_ok:
            # Local branch doesn't exist — check if remote tracking branch does.
            try:
                self.run_git_command(
                    self.repoPath,
                    "show-ref",
                    "--verify",
                    "refs/remotes/origin/{}".format(self.branchName),
                )
                remote_ok = True
            except Exception:  # noqa: BLE001
                remote_ok = False

            if not remote_ok:
                raise RuntimeError(
                    "branch {} not found locally or on remote".format(self.branchName)
                )
            # Create a local tracking branch via worktree add -b. On failure
            # OR interruption (Ctrl-C mid-add), roll the partial worktree —
            # and the branch the add may have just created — back out so the
            # repo is untouched and the title can be retried.
            try:
                self.run_git_command(
                    self.repoPath,
                    "worktree",
                    "add",
                    "-b",
                    self.branchName,
                    self.worktreePath,
                    "origin/{}".format(self.branchName),
                )
            except BaseException as err:
                self._rollback_partial_worktree(delete_branch=True)
                if isinstance(err, Exception):
                    raise RuntimeError(
                        "failed to create worktree from remote branch {}: {}".format(
                            self.branchName, err
                        )
                    ) from err
                raise  # KeyboardInterrupt/SystemExit: rolled back, re-raise as-is
            return

        # Create a new worktree from the existing local branch. The branch is
        # pre-existing, so rollback removes only the worktree, never the branch.
        try:
            self.run_git_command(
                self.repoPath, "worktree", "add", self.worktreePath, self.branchName
            )
        except BaseException as err:
            self._rollback_partial_worktree(delete_branch=False)
            if isinstance(err, Exception):
                raise RuntimeError(
                    "failed to create worktree from branch {}: {}".format(
                        self.branchName, err
                    )
                ) from err
            raise

    # --- setupNewWorktree -------------------------------------------------
    def setup_new_worktree(self) -> None:
        """Create a new worktree from the current HEAD commit."""
        # Clean up any existing worktree first (ignore error if absent).
        try:
            self.run_git_command(
                self.repoPath, "worktree", "remove", "-f", self.worktreePath
            )
        except Exception:  # noqa: BLE001
            pass
        # If the directory is still there (orphaned), drop it.
        _safe_rmtree(self.worktreePath, self.repoPath)

        # Clean up any existing branch (ignore error if it doesn't exist).
        try:
            self.run_git_command(self.repoPath, "branch", "-D", self.branchName)
        except Exception:  # noqa: BLE001
            pass

        try:
            output = self.run_git_command(self.repoPath, "rev-parse", "HEAD")
        except Exception as err:  # noqa: BLE001
            msg = str(err)
            if (
                "fatal: ambiguous argument 'HEAD'" in msg
                or "fatal: not a valid object name" in msg
                or "fatal: HEAD: not a valid object name" in msg
            ):
                raise RuntimeError(
                    "this appears to be a brand new repository: please create an "
                    "initial commit before creating an instance"
                ) from err
            raise RuntimeError(
                "failed to get HEAD commit hash: {}".format(err)
            ) from err

        head_commit = output.strip()
        self.baseCommitSHA = head_commit

        # Create a new worktree from the HEAD commit so we start clean (no
        # uncommitted changes inherited from the previous worktree). On failure
        # OR interruption (Ctrl-C mid-add), roll the partial worktree and the
        # just-created branch back out so the repo is untouched and the same
        # session title can be retried immediately.
        try:
            self.run_git_command(
                self.repoPath,
                "worktree",
                "add",
                "-b",
                self.branchName,
                self.worktreePath,
                head_commit,
            )
        except BaseException as err:
            self._rollback_partial_worktree(delete_branch=True)
            if isinstance(err, Exception):
                raise RuntimeError(
                    "failed to create worktree from commit {}: {}".format(
                        head_commit, err
                    )
                ) from err
            raise  # KeyboardInterrupt/SystemExit: rolled back, re-raise as-is

    # --- Cleanup ----------------------------------------------------------
    def Cleanup(self) -> None:
        """Remove the worktree and (unless pre-existing) its branch, then prune.

        Collects errors and joins them via ``combine_errors`` so multiple
        failures surface together.
        """
        errs = []

        # Check if worktree path exists before attempting removal.
        path_exists = True
        stat_err = None
        try:
            os.stat(self.worktreePath)
        except FileNotFoundError:
            path_exists = False
        except OSError as err:
            path_exists = False
            stat_err = err

        if path_exists:
            try:
                self.run_git_command(
                    self.repoPath, "worktree", "remove", "-f", self.worktreePath
                )
            except Exception as err:  # noqa: BLE001
                errs.append(err)
        elif stat_err is not None:
            # Only append error if it's not a "not exists" error.
            errs.append(
                RuntimeError("failed to check worktree path: {}".format(stat_err))
            )

        # Delete the branch using git CLI, but skip if pre-existing.
        if not self.isExistingBranch:
            try:
                self.run_git_command(self.repoPath, "branch", "-D", self.branchName)
            except Exception as err:  # noqa: BLE001
                # Only record if it's not a "branch not found" error.
                if "not found" not in str(err):
                    errs.append(
                        RuntimeError(
                            "failed to remove branch {}: {}".format(
                                self.branchName, err
                            )
                        )
                    )

        # Prune the worktree to clean up any remaining references.
        try:
            self.Prune()
        except Exception as err:  # noqa: BLE001
            errs.append(err)

        if len(errs) > 0:
            raise self.combine_errors(errs)

    # --- Remove -----------------------------------------------------------
    def Remove(self) -> None:
        """Remove the worktree but keep the branch."""
        try:
            self.run_git_command(
                self.repoPath, "worktree", "remove", "-f", self.worktreePath
            )
        except Exception as err:  # noqa: BLE001
            raise RuntimeError("failed to remove worktree: {}".format(err)) from err

    # --- Prune ------------------------------------------------------------
    def Prune(self) -> None:
        """Remove all working-tree administrative files via ``worktree prune``."""
        try:
            self.run_git_command(self.repoPath, "worktree", "prune")
        except Exception as err:  # noqa: BLE001
            raise RuntimeError("failed to prune worktrees: {}".format(err)) from err

    # snake_case aliases
    setup = Setup
    cleanup = Cleanup
    remove = Remove
    prune = Prune


# ---------------------------------------------------------------------------
# CleanupWorktrees (free function)
# ---------------------------------------------------------------------------
def cleanup_worktrees(repo_path: str | None = None) -> None:
    """Remove all worktrees and their associated branches.

    Reads the worktrees directory, maps registered worktrees to their branch
    names via ``git -C <repo> worktree list --porcelain``, deletes each
    directory's branch and the directory itself, then ``git worktree prune``.
    Branch-delete failures are logged but do not abort.

    ``repo_path`` is the repository the worktrees belong to. When omitted it
    defaults to the process CWD (the historical behavior) — but callers should
    pass it explicitly so the git commands don't silently run against whatever
    directory the server process happens to be in.
    """
    from backend.session.git.worktree import get_worktree_directory

    repo = repo_path or os.getcwd()

    try:
        worktrees_dir = get_worktree_directory()
    except Exception as err:  # noqa: BLE001
        raise RuntimeError("failed to get worktree directory: {}".format(err)) from err

    try:
        entries = list(os.scandir(worktrees_dir))
    except OSError as err:
        raise RuntimeError("failed to read worktree directory: {}".format(err)) from err

    # Get a list of all branches associated with worktrees.
    try:
        cmd = subprocess.run(
            ["git", "-C", repo, "worktree", "list", "--porcelain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except subprocess.TimeoutExpired as err:
        raise RuntimeError("failed to list worktrees: timed out after 60s") from err
    if cmd.returncode != 0:
        raise RuntimeError(
            "failed to list worktrees: {}".format(_exit_error(cmd.returncode))
        )
    output = cmd.stdout.decode("utf-8", "replace")

    # Parse the output to extract branch names.
    worktree_branches = {}
    current_worktree = ""
    for line in output.split("\n"):
        if line.startswith("worktree "):
            current_worktree = _trim_prefix(line, "worktree ")
        elif line.startswith("branch "):
            branch_path = _trim_prefix(line, "branch ")
            # Extract branch name from refs/heads/branch-name.
            branch_name = _trim_prefix(branch_path, "refs/heads/")
            if current_worktree != "":
                worktree_branches[current_worktree] = branch_name

    for entry in entries:
        if entry.is_dir():
            worktree_path = os.path.join(worktrees_dir, entry.name)

            # Delete the branch associated with this worktree if found.
            # Exact path match: a substring test (`entry.name in path`) can
            # match ANOTHER session whose path merely contains this name and
            # delete the wrong branch.
            wt_real = os.path.realpath(worktree_path)
            for path, branch in worktree_branches.items():
                if os.path.realpath(path) == wt_real:
                    try:
                        delete_cmd = subprocess.run(
                            ["git", "-C", repo, "branch", "-D", branch],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=60,
                        )
                        delete_err = (
                            _exit_error(delete_cmd.returncode)
                            if delete_cmd.returncode != 0
                            else None
                        )
                    except subprocess.TimeoutExpired:
                        delete_err = "timed out after 60s"
                    if delete_err is not None:
                        # Log the error but continue with other worktrees.
                        if log.ErrorLog is not None:
                            log.ErrorLog.Printf(
                                "failed to delete branch %s: %v",
                                branch,
                                delete_err,
                            )
                    break

            # Remove the worktree directory (guarded against catastrophic
            # targets, like every other teardown path in this module).
            _safe_rmtree(worktree_path, os.getcwd())

    # You have to prune the cleaned up worktrees.
    try:
        cmd = subprocess.run(
            ["git", "-C", repo, "worktree", "prune"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except subprocess.TimeoutExpired as err:
        raise RuntimeError("failed to prune worktrees: timed out after 60s") from err
    if cmd.returncode != 0:
        raise RuntimeError(
            "failed to prune worktrees: {}".format(_exit_error(cmd.returncode))
        )


# Go-name alias.
CleanupWorktrees = cleanup_worktrees
