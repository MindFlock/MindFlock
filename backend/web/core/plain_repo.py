"""Base-folder selection for plain (non-provisioned) sessions.

Resolving — and if needed creating / ``git init``-ing — the folder a plain
session runs in (:func:`_prepare_plain_repo`), plus the preflight that refuses
to fork a worktree off a repo mid-rebase/merge/cherry-pick/bisect
(:func:`_raise_on_blocked_repo`). A git repo is NOT required to start a
session: a plain folder simply runs in-place with git features off.

Split out of ``backend.web.server`` (which re-imports these names — the
create-instance route and tests reference them through the server namespace).
"""

from __future__ import annotations

import os

from backend import log


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


def _prepare_plain_repo(repo_path: str, init_repo: bool) -> tuple[str, bool]:
    """Resolve (and if needed create) the base folder for a plain session.

    Returns ``(abs_path, git_enabled)``. ``git_enabled`` is True when the folder
    is a git repo with at least one commit — so the worktree model and the
    diff/commit/PR features work; False for a plain, non-git folder, where the
    session simply runs in-place with git features turned off. **A git repo is
    NOT required to start a session.** Raises ``ValueError`` only when the folder
    is genuinely unusable (blank path, or it can't be created/initialised).
    """
    srv = _server()
    if not repo_path or not repo_path.strip():
        raise ValueError("a folder is required (pick one, or type a path to create)")
    abs_path = os.path.realpath(os.path.expanduser(repo_path.strip()))
    exists = os.path.isdir(abs_path)

    if init_repo:
        # Create-from-scratch (or adopt a plain dir): mkdir + git init + commit.
        if not srv.git_available():
            raise ValueError(
                "git is not installed, so a repo can't be created here — "
                "install git, or start the session in the plain folder"
            )
        if not exists:
            try:
                os.makedirs(abs_path, exist_ok=True)
            except OSError as err:
                raise ValueError("could not create folder %s: %s" % (abs_path, err))
        if not srv._is_git_repo(abs_path):
            r = srv._run_capped(
                ["git", "init", abs_path], capture_output=True, text=True, timeout=60
            )
            if r.returncode != 0:
                raise ValueError("git init failed: " + (r.stderr or r.stdout).strip())
        if not srv._git_has_commits(abs_path):
            srv._make_initial_commit(abs_path)
        srv._raise_on_blocked_repo(abs_path)
        return abs_path, True

    # Open (or create) a plain folder. No git is required.
    if not exists:
        # The user pointed at a path that doesn't exist yet — create it as a
        # plain scratch folder (git features stay off until they init a repo).
        try:
            os.makedirs(abs_path, exist_ok=True)
        except OSError as err:
            raise ValueError("could not create folder %s: %s" % (abs_path, err))
    if not srv._is_git_repo(abs_path):
        # A perfectly good workspace — just without git features.
        return abs_path, False
    if not srv._git_has_commits(abs_path):
        # Empty repo (no commits) — a worktree off HEAD needs one.
        srv._make_initial_commit(abs_path)
    srv._raise_on_blocked_repo(abs_path)
    return abs_path, True


def _raise_on_blocked_repo(abs_path: str) -> None:
    """Fail session creation loudly when the repo is mid-operation.

    A rebase/merge/cherry-pick/bisect in progress means HEAD is half-finished;
    forking a worktree off it creates a session the user didn't mean. The
    preflight message names the operation and the exact command that resolves
    it, and nothing has been written yet, so the repo is untouched. Warnings
    (detached HEAD, shallow clone) are logged, not raised."""
    from backend.session import preflight

    issues = preflight.repo_issues(abs_path)
    blockers = [i for i in issues if i.severity == "block"]
    if blockers:
        raise ValueError(" ".join(i.render() for i in blockers))
    for issue in issues:  # warnings only, at this point
        if log.WarningLog is not None:
            log.WarningLog.Printf("preflight %s: %s", issue.code, issue.render())
