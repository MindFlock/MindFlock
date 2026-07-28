"""Hermetic tests for :mod:`backend.session.git.util`.

These pin the Go-parity byte-exact error strings and the branch-name
sanitisation rules. External commands (``gh``, ``git``) are mocked at the
:mod:`subprocess` / :func:`shutil.which` seam so nothing real is spawned and no
network / auth is touched.
"""

from __future__ import annotations

import subprocess

import pytest

from backend.session.git import util
from backend.session.git.util import (
    check_gh_cli,
    find_git_repo_root,
    is_git_repo,
    sanitize_branch_name,
)


# ---------------------------------------------------------------------------
# sanitize_branch_name — pure transform (Go sanitizeBranchName parity)
# ---------------------------------------------------------------------------
def test_sanitize_lowercases_and_spaces_to_dash():
    assert sanitize_branch_name("Feature Login") == "feature-login"


def test_sanitize_drops_disallowed_chars():
    # '@', '#', '!' are outside [a-z0-9\-_/.] and are removed entirely.
    assert sanitize_branch_name("feat@#!ure") == "feature"


def test_sanitize_collapses_dash_runs():
    assert sanitize_branch_name("a  -  b") == "a-b"


def test_sanitize_trims_leading_trailing_dash_and_slash():
    assert sanitize_branch_name("/-foo/bar-/") == "foo/bar"


def test_sanitize_keeps_slash_dot_underscore():
    assert sanitize_branch_name("feature/sc-42.1_x") == "feature/sc-42.1_x"


def test_sanitize_backslash_domain_user_is_stripped():
    # A Windows domain user "DOMAIN\user" loses the backslash (disallowed).
    assert sanitize_branch_name("DOMAIN\\user") == "domainuser"


def test_sanitize_go_aliases_are_same_function():
    assert util.sanitizeBranchName is util.sanitize_branch_name
    assert util.checkGHCLI is util.check_gh_cli
    assert util.IsGitRepo is util.is_git_repo
    assert util.findGitRepoRoot is util.find_git_repo_root


# ---------------------------------------------------------------------------
# check_gh_cli — exact Go error strings
# ---------------------------------------------------------------------------
def test_check_gh_cli_not_installed_raises(monkeypatch):
    monkeypatch.setattr(util.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError) as ei:
        check_gh_cli()
    assert str(ei.value) == (
        "GitHub CLI (gh) is not installed. Please install it first"
    )


def test_check_gh_cli_not_configured_raises(monkeypatch):
    monkeypatch.setattr(util.shutil, "which", lambda _: "/usr/bin/gh")

    class _R:
        returncode = 1

    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(RuntimeError) as ei:
        check_gh_cli()
    assert str(ei.value) == (
        "GitHub CLI is not configured. Please run 'gh auth login' first"
    )


def test_check_gh_cli_success_returns_none(monkeypatch):
    monkeypatch.setattr(util.shutil, "which", lambda _: "/usr/bin/gh")

    class _R:
        returncode = 0

    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: _R())
    assert check_gh_cli() is None


def test_check_gh_cli_timeout_raises(monkeypatch):
    monkeypatch.setattr(util.shutil, "which", lambda _: "/usr/bin/gh")

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gh auth status", timeout=30)

    monkeypatch.setattr(util.subprocess, "run", _boom)
    with pytest.raises(RuntimeError) as ei:
        check_gh_cli()
    assert str(ei.value) == "GitHub CLI check timed out after 30s (gh auth status)"


# ---------------------------------------------------------------------------
# is_git_repo — returncode -> bool, timeout -> False
# ---------------------------------------------------------------------------
def test_is_git_repo_true_on_clean_exit(monkeypatch):
    class _R:
        returncode = 0

    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: _R())
    assert is_git_repo("/some/path") is True


def test_is_git_repo_false_on_nonzero(monkeypatch):
    class _R:
        returncode = 128

    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: _R())
    assert is_git_repo("/some/path") is False


def test_is_git_repo_false_on_timeout(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    monkeypatch.setattr(util.subprocess, "run", _boom)
    assert is_git_repo("/some/path") is False


def test_is_git_repo_argv_uses_dash_c(monkeypatch):
    seen = {}

    class _R:
        returncode = 0

    def _capture(argv, *a, **k):
        seen["argv"] = argv
        return _R()

    monkeypatch.setattr(util.subprocess, "run", _capture)
    is_git_repo("/repo/x")
    assert seen["argv"] == [
        "git",
        "-C",
        "/repo/x",
        "rev-parse",
        "--show-toplevel",
    ]


# ---------------------------------------------------------------------------
# find_git_repo_root — trimmed stdout / exact failure message
# ---------------------------------------------------------------------------
def test_find_git_repo_root_returns_trimmed_stdout(monkeypatch):
    class _R:
        returncode = 0
        stdout = b"/home/user/repo\n"

    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: _R())
    assert find_git_repo_root("/home/user/repo/sub") == "/home/user/repo"


def test_find_git_repo_root_failure_raises_with_path(monkeypatch):
    class _R:
        returncode = 128
        stdout = b""

    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(RuntimeError) as ei:
        find_git_repo_root("/not/a/repo")
    assert str(ei.value) == (
        "failed to find Git repository root from path: /not/a/repo"
    )


def test_find_git_repo_root_timeout_raises_with_path(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    monkeypatch.setattr(util.subprocess, "run", _boom)
    with pytest.raises(RuntimeError) as ei:
        find_git_repo_root("/hang")
    assert str(ei.value) == ("failed to find Git repository root from path: /hang")
