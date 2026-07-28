"""Port of the Go ``session/instance.go``.

Defines the :class:`Instance` type: a running or paused Claude Code session that
manages a tmux session, a git worktree, and associated metadata. Supports the
lifecycle operations start / pause / resume / kill, git diff statistics, and
tmux interaction (send prompt / keys, capture pane content, resize).

Error-convention bridge:
  * The git worktree port (``session.git``) *raises* on failure (and getters /
    predicates return values).
  * The tmux port (``session.tmux``) returns ``Optional[Exception]`` Go-style.
This module converts both into Python idioms, raising ``RuntimeError`` with the
same wrapped messages the Go source produces.

The serialization dataclasses (``InstanceData`` / ``GitWorktreeData`` /
``DiffStatsData``) and the ``Status`` enum live in :mod:`.storage`; this module
imports them from there to avoid a circular import.
"""

from __future__ import annotations

import datetime as _datetime
import os
import subprocess
import time
from typing import List, Optional, Tuple

import pyperclip

from backend import log
from backend.session import git
from backend.session import tmux
from backend.session.storage import (
    DiffStatsData,
    GitWorktreeData,
    InstanceData,
    Loading,
    Paused,
    Ready,
    Running,
    Status,
)

__all__ = [
    "Status",
    "Running",
    "Ready",
    "Loading",
    "Paused",
    "Instance",
    "InstanceOptions",
    "NewInstance",
    "new_instance",
    "FromInstanceData",
    "from_instance_data",
]


def _err_text(err: BaseException) -> str:
    """Render an exception like Go's ``err.Error()`` (its message only)."""
    return str(err)


# Seconds to wait on the short ``git rev-parse`` / ``symbolic-ref`` probes used to
# detect a worktree's current branch and base commit.
_GIT_PROBE_TIMEOUT_SECONDS = 30


class InstanceOptions:
    """Options for creating a new instance (Go ``InstanceOptions``).

    ``Branch`` is an existing branch name to start the session on; empty means a
    new branch from HEAD. ``AutoYes`` is currently ignored (the instance is
    created with ``AutoYes=False``), matching the Go source.

    Provisioned-workspace extension (all optional, off by default — ordinary
    sessions are unaffected):
      * ``provisioned``        - provision a fully-loaded workspace (setup
        commands + warm cache seeds; see :mod:`backend.session.provisioned`).
      * ``workspace_strategy`` - ``"worktree"`` (default) or ``"clone"``.
      * ``provision_repo``     - provision THIS local git repo instead of the
        configured ``[repository].url`` (the universal any-repo flow).
      * ``new_branch``         - explicit new branch name to create (overrides
        the ``<branch_prefix><title>`` scheme); used for Shortcut-style
        ``feature/sc-<id>/<slug>`` names.
      * ``prompt``             - initial prompt to seed the agent with on launch.
      * ``launch_args``        - per-session argv tokens appended on every
        launch/relaunch, after provider defaults. ``None`` (default) = not
        specified, so the session inherits the global default launch flags
        (``coding_cli.default_launch_args``); a list/tuple (even empty) is
        explicit and used verbatim (the global default is NOT re-applied).
      * ``workspace_path``     - adopt an already-provisioned directory.
    """

    def __init__(
        self,
        title: str = "",
        path: str = "",
        program: str = "",
        auto_yes: bool = False,
        branch: str = "",
        provisioned: bool = False,
        workspace_strategy: str = "worktree",
        new_branch: str = "",
        prompt: str = "",
        launch_args=None,
        workspace_path: str = "",
        provision_repo: str = "",
        provision_repo_url: str = "",
        in_place: bool = False,
    ) -> None:
        self.title = title
        self.path = path
        self.program = program
        self.auto_yes = auto_yes
        self.branch = branch
        self.provisioned = provisioned
        self.workspace_strategy = workspace_strategy
        self.new_branch = new_branch
        self.prompt = prompt
        # None = "not specified" -> new_instance applies the global default
        # launch flags; a list/tuple (even empty) = explicit, used verbatim so a
        # default the user toggled off for one session is honored.
        self.launch_args = None if launch_args is None else tuple(launch_args)
        # Adopt an already-provisioned directory (e.g. a PR workspace) instead
        # of creating a fresh worktree/clone. Branch is taken from new_branch.
        self.workspace_path = workspace_path
        # Universal provisioning: the local repo to provision (empty = the
        # configured [repository].url).
        self.provision_repo = provision_repo
        # Multi-repo ticket ingestion: a remote clone URL to provision this ONE
        # session from, overriding [repository].url (empty = use the config).
        # Unlike provision_repo (a local path), this is a URL the engine clones.
        self.provision_repo_url = provision_repo_url
        # Run the session DIRECTLY in ``path`` (an existing repo) with NO
        # worktree and NO new branch — so several sessions share the same
        # working copy. Cleanup never deletes the directory.
        self.in_place = in_place


class Instance:
    """A running (or paused) instance of Claude Code (Go ``Instance``).

    Exported fields mirror the Go struct; unexported state (``_started``,
    ``_tmux_session``, ``_git_worktree``, ``_diff_stats``, ``_selected_branch``)
    is initialized during :meth:`Start` / :func:`FromInstanceData`.
    """

    def __init__(self) -> None:
        # Exported fields.
        self.Title: str = ""
        self.Path: str = ""
        self.Branch: str = ""
        self.Status: Status = Status.Running
        self.Program: str = ""
        self.Height: int = 0
        self.Width: int = 0
        self.CreatedAt: _datetime.datetime = None  # type: ignore[assignment]
        self.UpdatedAt: _datetime.datetime = None  # type: ignore[assignment]
        self.AutoYes: bool = False
        self.Prompt: str = ""
        self.LaunchArgs: tuple[str, ...] = ()
        # Extra env vars for the agent's tmux session (O4 port block). Set by
        # the web server before Start/Resume; not persisted — the server
        # re-derives it from its ports allocation on every (re)start.
        self.ExtraEnv: dict = {}
        # Provisioned-workspace extension (off by default).
        self.Provisioned: bool = False
        self.WorkspaceStrategy: str = "worktree"
        # In-place mode: run directly in an existing repo, no worktree.
        self.InPlace: bool = False
        # Branch this session's worktree was cut from, recorded at first Start
        # (per-session diff/stage base). "" when unrecorded — readers resolve a
        # base via a fallback chain (origin/HEAD -> main/master -> configured
        # base).
        self.BaseBranch: str = ""

        # Unexported fields.
        self._diff_stats: Optional[git.DiffStats] = None
        self._selected_branch: str = ""
        self._new_branch: str = ""
        self._workspace_path: str = ""
        self._provision_repo: str = ""
        self._provision_repo_url: str = ""
        self._started: bool = False
        self._tmux_session: Optional[tmux.TmuxSession] = None
        self._git_worktree: Optional[git.GitWorktree] = None

    # --- Serialization ------------------------------------------------------
    def ToInstanceData(self) -> InstanceData:
        """Convert to the serializable :class:`InstanceData` (Go ``ToInstanceData``).

        ``updated_at`` is set to *now* (overriding the stored value). The
        ``worktree`` block is filled from the git worktree only if one is
        initialized; ``diff_stats`` only if diff stats exist. Otherwise each is
        left as a zero-value object (always present in JSON).
        """
        data = InstanceData(
            title=self.Title,
            path=self.Path,
            branch=self.Branch,
            status=self.Status,
            height=self.Height,
            width=self.Width,
            created_at=self.CreatedAt,
            updated_at=_datetime.datetime.now(_datetime.timezone.utc).astimezone(),
            program=self.Program,
            launch_args=tuple(getattr(self, "LaunchArgs", ()) or ()),
            auto_yes=self.AutoYes,
            provisioned=self.Provisioned,
            workspace_strategy=self.WorkspaceStrategy,
            in_place=self.InPlace,
            base_branch=self.BaseBranch,
        )

        if self._git_worktree is not None:
            data.worktree = GitWorktreeData(
                repo_path=self._git_worktree.GetRepoPath(),
                worktree_path=self._git_worktree.GetWorktreePath(),
                session_name=self.Title,
                branch_name=self._git_worktree.GetBranchName(),
                base_commit_sha=self._git_worktree.GetBaseCommitSHA(),
                is_existing_branch=self._git_worktree.IsExistingBranch(),
            )

        if self._diff_stats is not None:
            data.diff_stats = DiffStatsData(
                added=self._diff_stats.Added,
                removed=self._diff_stats.Removed,
                content=self._diff_stats.Content,
            )

        return data

    # --- Lifecycle ----------------------------------------------------------
    def SetStatus(self, status: Status) -> None:
        """Set the instance status (Go ``SetStatus``)."""
        self.Status = status

    def Start(self, first_time_setup: bool) -> None:
        """Initialize the git worktree and tmux session (Go ``Start``).

        ``first_time_setup=True`` creates a fresh worktree (from the selected
        branch, or a new branch off HEAD) and starts a new tmux session.
        ``False`` restores an existing session. On success ``_started`` is set
        and the status becomes ``Running``. On failure all resources are cleaned
        up (via :meth:`Kill`) and a ``RuntimeError`` is raised; cleanup errors
        are chained as ``"<orig> (cleanup error: <cleanup>)"``.
        """
        if self.Title == "":
            raise RuntimeError("instance title cannot be empty")

        if self._tmux_session is not None:
            # Use existing tmux session (useful for testing).
            tmux_session = self._tmux_session
        else:
            tmux_session = tmux.NewTmuxSession(self.Title, self.Program)
        self._tmux_session = tmux_session

        if first_time_setup:
            self._create_first_time_worktree()

        # Mirror Go's deferred error handler: run cleanup on failure, set
        # _started only on complete success.
        setup_err: Optional[BaseException] = None
        try:
            if not first_time_setup:
                err = tmux_session.restore()
                if err is not None:
                    setup_err = RuntimeError(
                        "failed to restore existing session: {}".format(_err_text(err))
                    )
                    raise setup_err
            else:
                # Setup git worktree first.
                try:
                    self._git_worktree.Setup()
                except Exception as err:  # noqa: BLE001
                    setup_err = RuntimeError(
                        "failed to setup git worktree: {}".format(_err_text(err))
                    )
                    raise setup_err from err

                # Resolve the coding provider and configure the launch
                # command now that the worktree exists and before tmux starts.
                self._configure_launch_command()

                # Create new tmux session.
                if self.ExtraEnv:
                    self._tmux_session.extra_env = dict(self.ExtraEnv)
                start_err = self._tmux_session.start(
                    self._git_worktree.GetWorktreePath()
                )
                if start_err is not None:
                    # Cleanup git worktree if tmux session creation fails.
                    try:
                        self._git_worktree.Cleanup()
                    except Exception as cleanup_err:  # noqa: BLE001
                        start_err = Exception(
                            "{} (cleanup error: {})".format(
                                _err_text(start_err), _err_text(cleanup_err)
                            )
                        )
                    setup_err = RuntimeError(
                        "failed to start new session: {}".format(_err_text(start_err))
                    )
                    raise setup_err

            self.SetStatus(Status.Running)
        except BaseException as raised:  # noqa: BLE001
            if setup_err is None:
                setup_err = raised
            try:
                if self._started:
                    self.Kill()
                else:
                    # Start never completed, so Kill() would no-op (it guards on
                    # _started) and leak the workspace. In provisioned mode the
                    # failure-prone heavy work (full clone / worktree add + uv
                    # sync) happens inside worktree.Setup(), so a partial clone /
                    # registered worktree may exist — clean it up directly.
                    self._cleanup_partial()
            except Exception as cleanup_err:  # noqa: BLE001
                setup_err = RuntimeError(
                    "{} (cleanup error: {})".format(
                        _err_text(setup_err), _err_text(cleanup_err)
                    )
                )
            raise setup_err
        else:
            self._started = True

    def _create_first_time_worktree(self) -> None:
        """Create the git worktree for a first-time :meth:`Start`.

        Chooses one of four workspace shapes based on the instance's options and
        sets ``self._git_worktree``, ``self.Branch`` and ``self.BaseBranch``
        accordingly:

          * provisioned - a fully-loaded workspace (worktree off a canonical
            clone, or a fresh clone) for the configured / chosen repo;
          * in-place    - run directly in the existing repo at ``self.Path``
            (no worktree, no new branch);
          * from-branch - a worktree checked out from an existing branch;
          * new-branch  - a worktree on a fresh branch cut from HEAD.

        Raises ``RuntimeError`` (wrapping the underlying error) on failure.
        """
        if self.Provisioned:
            # Optional provisioned mode: build a fully-loaded workspace
            # (worktree off a canonical clone, or a fresh clone) instead of
            # a plain worktree of self.Path. The provisioning itself runs
            # inside the worktree's Setup() below. The target repo is the
            # configured [repository].url, or — universal flow — any local
            # repo passed via provision_repo.
            from backend.session import provisioned as _prov

            if self._provision_repo:
                settings = _prov.local_settings_for(self._provision_repo)
                if settings is None:
                    raise RuntimeError(
                        "provisioned session requested for {} but it is "
                        "not a git repository".format(self._provision_repo)
                    )
            else:
                settings = _prov.load_provision_settings(
                    repo_url_override=self._provision_repo_url or None
                )
                if settings is None:
                    raise RuntimeError(
                        "provisioned session requested but no repo is "
                        "configured (config.toml with [repository].url) "
                        "and no local repo was chosen"
                    )
            branch = (
                self._new_branch
                or self._selected_branch
                or _prov.branch_name_for(None, self.Title)
            )
            try:
                git_worktree = _prov.build_provisioned_worktree(
                    self.WorkspaceStrategy,
                    branch,
                    self.Title,
                    settings,
                    workspace_path=(self._workspace_path or None),
                )
            except Exception as err:  # noqa: BLE001
                raise RuntimeError(
                    "failed to create provisioned worktree: {}".format(err)
                ) from err
            self._git_worktree = git_worktree
            self.Branch = git_worktree.GetBranchName()
            # K1: the branch this workspace forked from (per-session base).
            self.BaseBranch = settings.base_branch or ""
        elif self.InPlace:
            # Run directly in the existing repo at self.Path — no worktree,
            # no new branch. Cleanup/Remove are no-ops so the user's repo is
            # never deleted; several sessions can share the same directory.
            git_worktree = _InPlaceWorktree(
                repoPath=self.Path,
                worktreePath=self.Path,
                sessionName=self.Title,
                branchName="",
            )
            self._git_worktree = git_worktree
            self.Branch = git_worktree.GetBranchName()
            # K1: in-place sessions live ON their branch — that branch is
            # also the base, so "committed" keys off origin/<branch>.
            self.BaseBranch = self.Branch or ""
        elif self._selected_branch != "":
            try:
                git_worktree = git.NewGitWorktreeFromBranch(
                    self.Path, self._selected_branch, self.Title
                )
            except Exception as err:  # noqa: BLE001
                raise RuntimeError(
                    "failed to create git worktree from branch: {}".format(err)
                ) from err
            self._git_worktree = git_worktree
            self.Branch = self._selected_branch
            # K1: an existing branch is its own base (diff vs its origin).
            self.BaseBranch = self._selected_branch
        else:
            # K1: record the source repo's current branch BEFORE cutting the
            # worktree — that's the branch the new session forks from.
            self.BaseBranch = _current_branch_of(self.Path)
            try:
                git_worktree, branch_name = git.NewGitWorktree(self.Path, self.Title)
            except Exception as err:  # noqa: BLE001
                raise RuntimeError(
                    "failed to create git worktree: {}".format(err)
                ) from err
            self._git_worktree = git_worktree
            self.Branch = branch_name

    def _configure_launch_command(self) -> None:
        """Resolve the coding provider and set the tmux launch command.

        Called from :meth:`Start` after the git worktree is set up and before
        the tmux session is created. Reads ``self.Program``/``Prompt``/
        ``LaunchArgs``/``InPlace``/``Provisioned`` and the worktree/tmux
        session, and mutates only ``self._tmux_session.launch_command``. Both
        branches are best-effort: a failure here is logged and swallowed so the
        session falls back to running the bare program.
        """
        # Resolve the coding provider for this session and let it decide
        # the launch command. Both this engine path and the web UI's
        # ``_ensure_agent_session`` drive the SAME provider so they can't
        # drift. We set launch_command rather than program so program
        # stays the human binary name for has_updated() / trust-prompt
        # handling. tmux runs the command as a single argv element.
        # Best-effort: a failure here just falls back to running program.
        from backend import providers as _providers
        from backend.providers import LaunchContext as _LaunchContext

        _provider = _providers.resolve(self.Program)
        _wt_path = self._git_worktree.GetWorktreePath()
        _ctx = _LaunchContext(
            program=self.Program,
            workdir=_wt_path,
            prompt=self.Prompt,
            resume=False,
            skip_permissions=False,
            in_place=bool(getattr(self, "InPlace", False)),
            session_name=self._tmux_session.sanitized_name,
            launch_args=tuple(getattr(self, "LaunchArgs", ()) or ()),
        )
        if self.Provisioned:
            # Provisioned: launch via a wrapper script written into the
            # (now provisioned) workspace. The wrapper exports each
            # cache's env vars (e.g. TESTMON_ENV, so a warm testmon seed
            # stays valid for in-session commits) and seeds the ticket
            # prompt when one is set. The launcher is
            # Claude/MindFlock-owned and threads the program through, so
            # use the Claude provider regardless of self.Program
            # (preserves the default behaviour for any program).
            try:
                from backend import workspace_setup as _ws

                _scs = getattr(self._git_worktree, "_provision_settings", None)
                _skip = _scs.skip_permissions if _scs is not None else True
                _cache_env = (
                    _ws.merged_cache_env(_scs.caches) if _scs is not None else None
                )
                launcher = _providers.resolve("claude").write_launcher(
                    _LaunchContext(
                        program=self.Program or "claude",
                        workdir=_wt_path,
                        prompt=self.Prompt,
                        skip_permissions=_skip,
                        in_place=_ctx.in_place,
                        session_name=_ctx.session_name,
                        launch_args=_ctx.launch_args,
                        cache_env=_cache_env,
                    )
                )
                self._tmux_session.launch_command = launcher
            except Exception as err:  # noqa: BLE001
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("failed to write workspace launcher: %v", err)
        else:
            # Plain / in-place session (no launcher). The provider
            # builds the launch command; a custom program runs bare.
            # ``cmd is None`` means "use the bare program".
            try:
                _cmd = _provider.build_launch_command(_ctx)
                if _cmd is not None:
                    self._tmux_session.launch_command = _cmd
            except Exception as err:  # noqa: BLE001
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("failed to build launch command: %v", err)

    def _cleanup_partial(self) -> None:
        """Tear down resources created by a Start() that failed before completion.

        Mirrors :meth:`Kill` but without the ``_started`` guard, for the window
        where Start() raised mid-setup. Closes any tmux session and cleans up the
        git worktree / clone so a failed workspace provision does not leak a
        multi-hundred-MB clone or an orphaned worktree registration.
        """
        if self._tmux_session is not None:
            try:
                self._tmux_session.close()
            except Exception:  # noqa: BLE001
                pass
        if self._git_worktree is not None:
            self._git_worktree.Cleanup()

    def Kill(self) -> None:
        """Terminate the instance and clean up all resources (Go ``Kill``).

        Both the tmux session and git worktree are cleaned up even if one fails;
        errors are combined via :meth:`_combine_errors` and raised. A no-op if
        the instance was never started.
        """
        if not self._started:
            return

        errs: List[BaseException] = []

        # Clean up tmux session first (it is using the git worktree).
        if self._tmux_session is not None:
            err = self._tmux_session.close()
            if err is not None:
                errs.append(
                    RuntimeError(
                        "failed to close tmux session: {}".format(_err_text(err))
                    )
                )

        # Then clean up the git worktree.
        if self._git_worktree is not None:
            try:
                self._git_worktree.Cleanup()
            except Exception as err:  # noqa: BLE001
                errs.append(
                    RuntimeError(
                        "failed to cleanup git worktree: {}".format(_err_text(err))
                    )
                )

        combined = self._combine_errors(errs)
        if combined is not None:
            raise combined

    def _combine_errors(self, errs: List[BaseException]) -> Optional[BaseException]:
        """Combine multiple errors into one (Go ``combineErrors``).

        Returns None for an empty list, the single error for one, and a
        ``RuntimeError`` with the header ``"multiple cleanup errors occurred:"``
        followed by ``"\\n  - <err>"`` per error otherwise.
        """
        if len(errs) == 0:
            return None
        if len(errs) == 1:
            return errs[0]

        err_msg = "multiple cleanup errors occurred:"
        for err in errs:
            err_msg += "\n  - " + _err_text(err)
        return RuntimeError(err_msg)

    def Preview(self) -> str:
        """Return the current tmux pane content (Go ``Preview``).

        Returns an empty string if the instance is not started or is paused.
        """
        if not self._started or self.Status == Status.Paused:
            return ""
        content, err = self._tmux_session.capture_pane_content()
        if err is not None:
            raise RuntimeError(_err_text(err))
        return content

    def HasUpdated(self) -> Tuple[bool, bool]:
        """Return ``(updated, has_prompt)`` for the session (Go ``HasUpdated``).

        Returns ``(False, False)`` if not started.
        """
        if not self._started:
            return False, False
        return self._tmux_session.has_updated()

    def CheckAndHandleTrustPrompt(self) -> bool:
        """Dismiss the trust prompt for supported programs (Go ``CheckAndHandleTrustPrompt``).

        Returns False if not started, the tmux session is missing, or the
        program is not claude/aider (matched by suffix).
        """
        if not self._started or self._tmux_session is None:
            return False
        program = self.Program
        if not program.endswith(tmux.ProgramClaude) and not program.endswith(
            tmux.ProgramAider
        ):
            return False
        return self._tmux_session.check_and_handle_trust_prompt()

    def TapEnter(self) -> None:
        """Send Enter to the session if AutoYes is enabled (Go ``TapEnter``).

        Fire-and-forget: errors are logged, not raised.
        """
        if not self._started or not self.AutoYes:
            return
        err = self._tmux_session.tap_enter()
        if err is not None and log.ErrorLog is not None:
            log.ErrorLog.Printf("error tapping enter: %v", err)

    def Attach(self):
        """Attach to the live session (Go ``Attach``).

        Returns the detach channel/event from the tmux session. Raises if the
        instance has not been started.
        """
        if not self._started:
            raise RuntimeError("cannot attach instance that has not been started")
        ch, err = self._tmux_session.attach()
        if err is not None:
            raise RuntimeError(_err_text(err))
        return ch

    def GetGitWorktree(self) -> git.GitWorktree:
        """Return the git worktree (Go ``GetGitWorktree``).

        Raises if the instance has not been started.
        """
        if not self._started:
            raise RuntimeError(
                "cannot get git worktree for instance that has not been started"
            )
        return self._git_worktree

    def GetWorktreePath(self) -> str:
        """Return the worktree path, or "" if unavailable (Go ``GetWorktreePath``)."""
        if self._git_worktree is None:
            return ""
        return self._git_worktree.GetWorktreePath()

    def Started(self) -> bool:
        """Return whether :meth:`Start` has completed (Go ``Started``)."""
        return self._started

    def Paused(self) -> bool:
        """Return whether the status is ``Paused`` (Go ``Paused``)."""
        return self.Status == Status.Paused

    def Pause(self) -> None:
        """Stop the tmux session and remove the worktree, keeping the branch.

        Mirrors Go ``Pause`` exactly, including the orphaned-worktree fast path,
        the dirty-changes commit (message uses RFC822 time), early returns on
        commit/remove/prune failure, and copying the branch name to the
        clipboard before returning.
        """
        if not self._started:
            raise RuntimeError("cannot pause instance that has not been started")
        if self.Status == Status.Paused:
            raise RuntimeError("instance is already paused")

        errs: List[BaseException] = []

        # Orphaned-worktree handling: if the path or .git is missing, git can't
        # operate on it. Skip dirty check + Remove, prune metadata, then pause.
        valid = None
        try:
            valid = self._git_worktree.IsValidWorktree()
        except Exception as err:  # noqa: BLE001
            errs.append(RuntimeError("failed to validate worktree: {}".format(err)))
            if log.ErrorLog is not None:
                log.ErrorLog.Print(err)

        if valid is False:
            self._pause_orphaned_worktree(errs)
            combined = self._combine_errors(errs)
            if combined is not None:
                raise combined
            return

        # Check if there are any changes to commit.
        dirty = None
        try:
            dirty = self._git_worktree.IsDirty()
        except Exception as err:  # noqa: BLE001
            errs.append(
                RuntimeError("failed to check if worktree is dirty: {}".format(err))
            )
            if log.ErrorLog is not None:
                log.ErrorLog.Print(err)

        if dirty:
            # Commit changes locally (without pushing to GitHub).
            commit_msg = "[mindflock] update from '{}' on {} (paused)".format(
                self.Title, _format_rfc822(_datetime.datetime.now().astimezone())
            )
            try:
                self._git_worktree.CommitChanges(commit_msg)
            except Exception as err:  # noqa: BLE001
                errs.append(RuntimeError("failed to commit changes: {}".format(err)))
                if log.ErrorLog is not None:
                    log.ErrorLog.Print(err)
                # Return early if we can't commit to avoid corrupted state.
                combined = self._combine_errors(errs)
                if combined is not None:
                    raise combined
                return

        # Detach from tmux instead of closing to preserve session output.
        derr = self._tmux_session.detach_safely()
        if derr is not None:
            errs.append(
                RuntimeError(
                    "failed to detach tmux session: {}".format(_err_text(derr))
                )
            )
            if log.ErrorLog is not None:
                log.ErrorLog.Print(derr)
            # Continue with pause process even if detach fails.

        # Check if worktree exists before trying to remove it.
        path_exists = True
        try:
            os.stat(self._git_worktree.GetWorktreePath())
        except OSError:
            path_exists = False
        if path_exists:
            # Remove worktree but keep branch.
            try:
                self._git_worktree.Remove()
            except Exception as err:  # noqa: BLE001
                errs.append(
                    RuntimeError("failed to remove git worktree: {}".format(err))
                )
                if log.ErrorLog is not None:
                    log.ErrorLog.Print(err)
                combined = self._combine_errors(errs)
                if combined is not None:
                    raise combined
                return

            # Only prune if remove was successful.
            try:
                self._git_worktree.Prune()
            except Exception as err:  # noqa: BLE001
                errs.append(
                    RuntimeError("failed to prune git worktrees: {}".format(err))
                )
                if log.ErrorLog is not None:
                    log.ErrorLog.Print(err)
                combined = self._combine_errors(errs)
                if combined is not None:
                    raise combined
                return

        self.SetStatus(Status.Paused)
        _clipboard_write(self._git_worktree.GetBranchName())

        combined = self._combine_errors(errs)
        if combined is not None:
            if log.ErrorLog is not None:
                log.ErrorLog.Print(combined)
            raise combined

    def _pause_orphaned_worktree(self, errs: List[BaseException]) -> None:
        """Pause a session whose worktree is orphaned (path or ``.git`` missing).

        Git cannot operate on the worktree, so the dirty check and ``Remove``
        are skipped: detach tmux, drop any leftover directory (unless the
        worktree preserves it across pause, e.g. clone mode), prune the stale
        worktree metadata, mark the instance ``Paused`` and copy the branch name
        to the clipboard. Any failures are appended to ``errs`` (and logged); the
        caller combines and raises them.
        """
        if log.WarningLog is not None:
            log.WarningLog.Printf(
                "worktree at %s is orphaned; skipping dirty check and remove",
                self._git_worktree.GetWorktreePath(),
            )
        derr = self._tmux_session.detach_safely()
        if derr is not None:
            errs.append(
                RuntimeError(
                    "failed to detach tmux session: {}".format(_err_text(derr))
                )
            )
            if log.ErrorLog is not None:
                log.ErrorLog.Print(derr)
        # Drop any leftover directory so a future Resume's `git worktree
        # add` won't conflict — UNLESS this worktree preserves its directory
        # across pause (clone mode: the branch + work live in the standalone
        # clone, so blowing it away would lose work and force a re-clone).
        keeps_dir = getattr(self._git_worktree, "keeps_dir_across_pause", None)
        if not (callable(keeps_dir) and keeps_dir()):
            try:
                _remove_all(self._git_worktree.GetWorktreePath())
            except Exception as err:  # noqa: BLE001
                errs.append(
                    RuntimeError(
                        "failed to remove orphaned worktree directory: {}".format(err)
                    )
                )
                if log.ErrorLog is not None:
                    log.ErrorLog.Print(err)
        try:
            self._git_worktree.Prune()
        except Exception as err:  # noqa: BLE001
            errs.append(RuntimeError("failed to prune git worktrees: {}".format(err)))
            if log.ErrorLog is not None:
                log.ErrorLog.Print(err)
        self.SetStatus(Status.Paused)
        _clipboard_write(self._git_worktree.GetBranchName())

    def Resume(self) -> None:
        """Recreate the worktree and restart the tmux session (Go ``Resume``).

        Raises if not started, not paused, or the branch is already checked out.
        Attempts to restore the existing tmux session, falling back to starting
        a new one (cleaning up the worktree on failure).
        """
        if not self._started:
            raise RuntimeError("cannot resume instance that has not been started")
        if self.Status != Status.Paused:
            raise RuntimeError("can only resume paused instances")

        # Check if branch is checked out.
        try:
            checked = self._git_worktree.IsBranchCheckedOut()
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Print(err)
            raise RuntimeError(
                "failed to check if branch is checked out: {}".format(err)
            ) from err
        if checked:
            raise RuntimeError(
                "cannot resume: branch is checked out, please switch to a "
                "different branch"
            )

        # Setup git worktree.
        try:
            self._git_worktree.Setup()
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Print(err)
            raise RuntimeError("failed to setup git worktree: {}".format(err)) from err

        # Restore existing tmux session if it exists, otherwise create new one.
        if self._tmux_session.does_session_exist():
            rerr = self._tmux_session.restore()
            if rerr is not None:
                if log.ErrorLog is not None:
                    log.ErrorLog.Print(rerr)
                # If restore fails, fall back to creating new session.
                self._start_tmux_or_cleanup()
        else:
            self._start_tmux_or_cleanup()

        self.SetStatus(Status.Running)

    def _start_tmux_or_cleanup(self) -> None:
        """Start a new tmux session, cleaning up the worktree on failure.

        Helper for :meth:`Resume`'s two start paths (which are identical in Go).
        Raises ``"failed to start new session: ..."`` chaining any cleanup error.
        """
        if self.ExtraEnv:
            self._tmux_session.extra_env = dict(self.ExtraEnv)
        start_err = self._tmux_session.start(self._git_worktree.GetWorktreePath())
        if start_err is not None:
            if log.ErrorLog is not None:
                log.ErrorLog.Print(start_err)
            try:
                self._git_worktree.Cleanup()
            except Exception as cleanup_err:  # noqa: BLE001
                start_err = Exception(
                    "{} (cleanup error: {})".format(
                        _err_text(start_err), _err_text(cleanup_err)
                    )
                )
                if log.ErrorLog is not None:
                    log.ErrorLog.Print(start_err)
            raise RuntimeError(
                "failed to start new session: {}".format(_err_text(start_err))
            )

    # --- Prompt / keys ------------------------------------------------------
    def SendPrompt(self, prompt: str) -> None:
        """Send a prompt then Enter to the session (Go ``SendPrompt``)."""
        if not self._started:
            raise RuntimeError("instance not started")
        if self._tmux_session is None:
            raise RuntimeError("tmux session not initialized")
        err = self._tmux_session.send_keys(prompt)
        if err is not None:
            raise RuntimeError(
                "error sending keys to tmux session: {}".format(_err_text(err))
            )

        # Brief pause to prevent carriage return from being interpreted as
        # newline.
        time.sleep(0.1)
        err = self._tmux_session.tap_enter()
        if err is not None:
            raise RuntimeError("error tapping enter: {}".format(_err_text(err)))

    def SendKeys(self, keys: str) -> None:
        """Send raw keys to the session (Go ``SendKeys``).

        Raises if not started or paused. Unlike :meth:`SendPrompt`, does not add
        Enter or sleep.
        """
        if not self._started or self.Status == Status.Paused:
            raise RuntimeError(
                "cannot send keys to instance that has not been started or is paused"
            )
        err = self._tmux_session.send_keys(keys)
        if err is not None:
            raise RuntimeError(_err_text(err))

    # snake_case aliases
    to_instance_data = ToInstanceData
    set_status = SetStatus
    start = Start
    kill = Kill
    preview = Preview
    has_updated = HasUpdated
    check_and_handle_trust_prompt = CheckAndHandleTrustPrompt
    tap_enter = TapEnter
    attach = Attach
    get_git_worktree = GetGitWorktree
    get_worktree_path = GetWorktreePath
    started = Started
    paused = Paused
    pause = Pause
    resume = Resume
    send_prompt = SendPrompt
    send_keys = SendKeys


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------
def _provider_default_launch_args(program: str) -> tuple[str, ...]:
    """Default launch flags for the provider that runs ``program`` — read from
    ``coding_cli.default_launch_args`` (a provider-name -> flags-string map).

    Launch flags are provider-specific (no flag is common to every CLI), so the
    default is looked up by the resolved provider's name: a default set for
    claude never leaks onto a codex session. The flags string is split into argv
    tokens with shell rules and validated with the same guard as per-session /
    provider args. Best-effort: any failure (no settings, malformed flags) yields
    no defaults so session creation never breaks."""
    try:
        import shlex

        from backend import providers as _providers
        from backend.config.settings import load_settings
        from backend.providers.config import validate_launch_args

        provider = _providers.resolve(program).name
        raw = load_settings().coding_cli.launch_args_for(provider).strip()
        if not raw:
            return ()
        return validate_launch_args(shlex.split(raw))
    except Exception:  # noqa: BLE001 — a bad default must not break creation
        return ()


def _merge_launch_args(*groups) -> tuple[str, ...]:
    """Concatenate arg groups, dropping duplicates while preserving first-seen
    order — so a flag set both globally and per-session appears exactly once."""
    seen: set = set()
    out: list = []
    for group in groups:
        for a in group or ():
            if a not in seen:
                seen.add(a)
                out.append(a)
    return tuple(out)


def new_instance(opts: InstanceOptions) -> Instance:
    """Create a new (unstarted) :class:`Instance` (Go ``NewInstance``).

    Captures the current time for ``CreatedAt``/``UpdatedAt``, resolves the path
    to absolute, sets ``Status=Ready`` and ``AutoYes=False`` (opts.AutoYes is
    intentionally ignored, matching Go), and stores ``opts.Branch`` as the
    selected branch.
    """
    t = _datetime.datetime.now().astimezone()

    try:
        abs_path = os.path.abspath(opts.path)
    except (OSError, ValueError) as err:
        raise RuntimeError("failed to get absolute path: {}".format(err)) from err

    inst = Instance()
    inst.Title = opts.title
    inst.Status = Status.Ready
    inst.Path = abs_path
    inst.Program = opts.program
    inst.Height = 0
    inst.Width = 0
    inst.CreatedAt = t
    inst.UpdatedAt = t
    inst.AutoYes = False
    # None = caller didn't specify -> inherit the global default launch flags;
    # an explicit list/tuple (even empty) is used verbatim so a per-session
    # toggle-off is honored and the global default is NOT re-applied.
    _la = getattr(opts, "launch_args", None)
    if _la is None:
        inst.LaunchArgs = _merge_launch_args(
            _provider_default_launch_args(opts.program)
        )
    else:
        inst.LaunchArgs = _merge_launch_args(tuple(_la))
    inst._selected_branch = opts.branch
    inst.Provisioned = opts.provisioned
    inst.WorkspaceStrategy = opts.workspace_strategy or "worktree"
    inst._new_branch = opts.new_branch
    inst.Prompt = opts.prompt
    inst._workspace_path = opts.workspace_path
    inst._provision_repo = opts.provision_repo
    inst._provision_repo_url = opts.provision_repo_url
    inst.InPlace = opts.in_place
    return inst


class _InPlaceWorktree(git.GitWorktree):
    """Run a session directly inside an existing repo — NO worktree, NO new branch.

    ``repoPath == worktreePath ==`` the user's folder. Git operations (diff,
    commit, push, dirty-check) work natively. ``Setup`` just records the current
    branch + a diff base (HEAD at session start); ``Remove`` / ``Prune`` /
    ``Cleanup`` are no-ops so the user's original repo is NEVER deleted, and the
    directory survives Pause. Multiple sessions can target the same folder.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._detect()

    def _detect(self) -> None:
        d = self.worktreePath
        try:
            r = subprocess.run(
                ["git", "-C", d, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=_GIT_PROBE_TIMEOUT_SECONDS,
            )
            if r.returncode == 0 and r.stdout.strip():
                self.branchName = r.stdout.strip()
        except Exception:  # noqa: BLE001
            pass
        if not self.baseCommitSHA:
            try:
                r = subprocess.run(
                    ["git", "-C", d, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=_GIT_PROBE_TIMEOUT_SECONDS,
                )
                if r.returncode == 0 and r.stdout.strip():
                    self.baseCommitSHA = r.stdout.strip()
            except Exception:  # noqa: BLE001
                pass

    def Setup(self) -> None:  # noqa: N802
        if not os.path.isdir(self.worktreePath):
            raise RuntimeError("in-place repo no longer exists: " + self.worktreePath)
        self._detect()

    def Remove(self) -> None:  # noqa: N802
        return None

    def Prune(self) -> None:  # noqa: N802
        return None

    def Cleanup(self) -> None:  # noqa: N802
        # Never delete the user's real repository.
        return None

    def IsBranchCheckedOut(self) -> bool:  # noqa: N802
        return False

    def keeps_dir_across_pause(self) -> bool:
        return True

    setup = Setup
    remove = Remove
    prune = Prune
    is_branch_checked_out = IsBranchCheckedOut
    cleanup = Cleanup


def _worktree_from_data(data: InstanceData):
    """Rebuild the git worktree for a persisted instance.

    For provisioned instances, reconstructs the matching provisioned worktree
    subclass (so Resume re-provisions the workspace); otherwise a plain
    ``GitWorktree``. Falls back to a plain worktree if provisioning config is
    no longer available.
    """
    fields = dict(
        repoPath=data.worktree.repo_path,
        worktreePath=data.worktree.worktree_path,
        sessionName=data.worktree.session_name,
        branchName=data.worktree.branch_name,
        baseCommitSHA=data.worktree.base_commit_sha,
        isExistingBranch=data.worktree.is_existing_branch,
    )
    if data.in_place:
        # MUST reconstruct the no-op subclass — a plain GitWorktree here would
        # let Kill/Cleanup delete the user's real repo.
        return _InPlaceWorktree(**fields)
    if data.provisioned:
        try:
            from backend.session import provisioned as _prov

            # Reconstruct the matching subclass even if settings are missing
            # (config moved): clone strategy in particular MUST keep its
            # clone-safe Remove/Prune/IsBranchCheckedOut overrides, or Resume of
            # a healthy session would wrongly trip the "branch already checked
            # out" guard. Setup() tolerates settings=None by skipping
            # provisioning. settings_for_workspace picks the configured repo's
            # settings when this workspace belongs to it, else local-repo
            # settings (universal flow).
            settings = _prov.settings_for_workspace(data.worktree.repo_path)
            if (data.workspace_strategy or "worktree") == "clone":
                return _prov.ProvisionedCloneWorktree(settings=settings, **fields)
            return _prov.ProvisionedWorktree(settings=settings, **fields)
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("failed to rebuild provisioned worktree: %v", err)
    return git.NewGitWorktreeFromStorage(
        data.worktree.repo_path,
        data.worktree.worktree_path,
        data.worktree.session_name,
        data.worktree.branch_name,
        data.worktree.base_commit_sha,
        data.worktree.is_existing_branch,
    )


def from_instance_data(data: InstanceData, attach: bool = True) -> Instance:
    """Reconstruct an :class:`Instance` from serialized data (Go ``FromInstanceData``).

    Builds the git worktree from the stored worktree fields and the diff stats
    from the stored diff fields. If the instance is paused, marks it started and
    creates (without starting) a tmux session; otherwise restores the running
    session via ``Start(False)``.

    ``attach=False`` reconstructs a *running* instance without re-attaching its
    tmux session (no server-side PTY is opened): it is marked started with a
    tmux-session object but no live attach. Used to surface sessions created by
    another process (e.g. MindFlock) in the web UI — the worktree (for diff) and
    the tmux name (for the terminal websocket, which attaches on its own) are
    all the UI needs.
    """
    inst = Instance()
    inst.Title = data.title
    inst.Path = data.path
    inst.Branch = data.branch
    inst.Status = data.status
    inst.Height = data.height
    inst.Width = data.width
    inst.CreatedAt = data.created_at
    inst.UpdatedAt = data.updated_at
    inst.Program = data.program
    inst.LaunchArgs = tuple(data.launch_args or ())
    inst.AutoYes = data.auto_yes
    inst.Provisioned = data.provisioned
    inst.WorkspaceStrategy = data.workspace_strategy or "worktree"
    inst.InPlace = data.in_place
    inst.BaseBranch = data.base_branch or ""
    inst._git_worktree = _worktree_from_data(data)
    inst._diff_stats = git.DiffStats(
        added=data.diff_stats.added,
        removed=data.diff_stats.removed,
        content=data.diff_stats.content,
    )

    if inst.Paused() or not attach:
        inst._started = True
        inst._tmux_session = tmux.NewTmuxSession(inst.Title, inst.Program)
    else:
        inst.Start(False)

    return inst


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _current_branch_of(repo_path: str) -> str:
    """Branch currently checked out at ``repo_path`` ("" if detached/error)."""
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_PROBE_TIMEOUT_SECONDS,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _remove_all(path: str) -> None:
    """Equivalent of Go's ``os.RemoveAll``: remove a path tree, ignoring absence."""
    import shutil

    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
        return
    if os.path.isdir(path):
        shutil.rmtree(path)


def _clipboard_write(text: str) -> None:
    """Equivalent of Go's ``clipboard.WriteAll``: best-effort, errors ignored."""
    try:
        pyperclip.copy(text)
    except Exception:  # noqa: BLE001
        pass


_RFC822_MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def _format_rfc822(t: _datetime.datetime) -> str:
    """Format ``t`` as Go's ``time.RFC822`` (layout ``"02 Jan 06 15:04 MST"``).

    Verified against the Go oracle, this is two-digit day, three-letter month,
    *two-digit* year, ``HH:MM`` (no seconds, no weekday), then the zone. Go uses
    the zone's abbreviation when the location is named (``EST``, ``UTC``) and a
    numeric offset (``-0500``) for an unnamed / fixed-offset zone.

    Python's ``datetime.tzname()`` returns a real abbreviation for the system
    local zone but a synthetic ``"UTC-05:00"`` for a fixed-offset ``timezone``;
    we treat the synthetic form (and ``None``) as "unnamed" and emit Go's
    numeric offset so the output matches byte-for-byte.
    """
    month = _RFC822_MONTHS[t.month - 1]
    base = "{dd:02d} {mon} {yy:02d} {hh:02d}:{mm:02d}".format(
        dd=t.day,
        mon=month,
        yy=t.year % 100,
        hh=t.hour,
        mm=t.minute,
    )

    zone = t.tzname()
    offset = t.utcoffset()
    # Treat None or a synthetic "UTC±HH:MM"/"UTC" fixed-offset name as unnamed.
    is_named = bool(zone) and not zone.startswith("UTC")
    if not is_named:
        if offset is None:
            zone = "UTC"
        elif offset == _datetime.timedelta(0):
            zone = "UTC"
        else:
            total_minutes = int(offset.total_seconds() // 60)
            sign = "+" if total_minutes >= 0 else "-"
            total_minutes = abs(total_minutes)
            zone = "{}{:02d}{:02d}".format(
                sign, total_minutes // 60, total_minutes % 60
            )
    return base + " " + zone


# Go-name aliases.
NewInstance = new_instance
FromInstanceData = from_instance_data
