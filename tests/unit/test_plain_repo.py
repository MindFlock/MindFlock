"""Unit tests for :mod:`backend.web.core.plain_repo`.

Base-folder resolution for plain (non-provisioned) sessions and the
mid-operation preflight guard. The git helpers and preflight are exercised
through the server facade seams (``monkeypatch.setattr(server, "_foo", …)``),
the same aliasing the module resolves at call time, so no real git repo, tmux,
or network is touched.
"""

from __future__ import annotations

import os

import pytest

from backend.session import preflight
from backend.web import server
from backend.web.core import plain_repo


@pytest.fixture()
def stub_git(monkeypatch):
    """Neutralise every git seam ``_prepare_plain_repo`` reaches through the
    server facade, so each test drives one branch deterministically."""
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_is_git_repo", lambda p: False)
    monkeypatch.setattr(server, "_git_has_commits", lambda p: False)
    monkeypatch.setattr(server, "_make_initial_commit", lambda p: None)
    monkeypatch.setattr(server, "_raise_on_blocked_repo", lambda p: None)


def test_blank_path_rejected(stub_git):
    with pytest.raises(ValueError, match="a folder is required"):
        plain_repo._prepare_plain_repo("   ", init_repo=False)


def test_init_repo_without_git_rejected(stub_git, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "git_available", lambda: False)
    with pytest.raises(ValueError, match="git is not installed"):
        plain_repo._prepare_plain_repo(str(tmp_path / "new"), init_repo=True)


def test_open_existing_plain_folder_git_disabled(stub_git, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    abs_path, git_enabled = plain_repo._prepare_plain_repo(str(d), init_repo=False)
    assert abs_path == os.path.realpath(str(d))
    assert git_enabled is False


def test_missing_path_created_as_plain_folder(stub_git, tmp_path):
    d = tmp_path / "does-not-exist-yet"
    abs_path, git_enabled = plain_repo._prepare_plain_repo(str(d), init_repo=False)
    assert os.path.isdir(abs_path)  # created on the fly
    assert git_enabled is False


def test_existing_git_repo_enables_git_and_runs_preflight(
    stub_git, monkeypatch, tmp_path
):
    d = tmp_path / "repo"
    d.mkdir()
    monkeypatch.setattr(server, "_is_git_repo", lambda p: True)
    monkeypatch.setattr(server, "_git_has_commits", lambda p: True)
    seen = []
    monkeypatch.setattr(server, "_raise_on_blocked_repo", lambda p: seen.append(p))
    abs_path, git_enabled = plain_repo._prepare_plain_repo(str(d), init_repo=False)
    assert git_enabled is True
    assert seen == [abs_path]  # the mid-operation preflight fired on this repo


def test_init_repo_makes_initial_commit_when_empty(stub_git, monkeypatch, tmp_path):
    d = tmp_path / "fresh"
    calls = {"init": 0, "commit": 0}

    class _R:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(server, "_is_git_repo", lambda p: False)
    monkeypatch.setattr(
        server,
        "_run_capped",
        lambda *a, **k: calls.__setitem__("init", calls["init"] + 1) or _R(),
    )
    monkeypatch.setattr(server, "_git_has_commits", lambda p: False)
    monkeypatch.setattr(
        server,
        "_make_initial_commit",
        lambda p: calls.__setitem__("commit", calls["commit"] + 1),
    )
    abs_path, git_enabled = plain_repo._prepare_plain_repo(str(d), init_repo=True)
    assert git_enabled is True
    assert os.path.isdir(abs_path)
    assert calls["init"] == 1 and calls["commit"] == 1


# --------------------------------------------------------------------------- #
# _raise_on_blocked_repo
# --------------------------------------------------------------------------- #
def _issue(severity, code="X", message="m", fix="f"):
    return preflight.Issue(code=code, severity=severity, message=message, fix=fix)


def test_blocked_repo_raises_with_rendered_message(monkeypatch):
    monkeypatch.setattr(
        preflight,
        "repo_issues",
        lambda p: [_issue("block", code="rebase", message="mid-rebase")],
    )
    with pytest.raises(ValueError, match="mid-rebase"):
        plain_repo._raise_on_blocked_repo("/somewhere")


def test_warnings_only_do_not_raise(monkeypatch):
    monkeypatch.setattr(
        preflight,
        "repo_issues",
        lambda p: [_issue("warn", code="detached", message="detached HEAD")],
    )
    # Warnings are logged, never raised.
    plain_repo._raise_on_blocked_repo("/somewhere")


def test_no_issues_does_not_raise(monkeypatch):
    monkeypatch.setattr(preflight, "repo_issues", lambda p: [])
    plain_repo._raise_on_blocked_repo("/somewhere")
