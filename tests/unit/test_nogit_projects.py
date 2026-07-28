"""_prepare_plain_repo: a git repo is NOT required to start a session.

Locks the productizing change that lets + New open any folder — a plain,
non-git folder runs in-place with git features simply disabled, while a git
repo (or ticking "create a git repo") keeps the full worktree + diff/PR flow.
"""

from __future__ import annotations

import subprocess

import pytest

from backend.web import server


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_blank_path_still_errors():
    with pytest.raises(ValueError):
        server._prepare_plain_repo("", False)


def test_non_git_folder_is_allowed_without_git(tmp_path):
    folder = tmp_path / "scratch"
    folder.mkdir()
    path, git_enabled = server._prepare_plain_repo(str(folder), False)
    assert path == str(folder.resolve())
    assert git_enabled is False  # runs in-place, git features off — no error


def test_missing_folder_is_created_as_plain_workspace(tmp_path):
    folder = tmp_path / "brand-new"
    assert not folder.exists()
    path, git_enabled = server._prepare_plain_repo(str(folder), False)
    assert folder.is_dir()  # created for the user
    assert git_enabled is False  # plain folder, no git required


def test_init_repo_makes_it_git_enabled(tmp_path):
    folder = tmp_path / "with-git"
    path, git_enabled = server._prepare_plain_repo(str(folder), True)
    assert git_enabled is True
    # It's a real repo with at least one commit (worktree off HEAD works).
    assert (folder / ".git").exists()
    head = subprocess.run(
        ["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    assert head.returncode == 0


def test_existing_git_repo_is_git_enabled(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    _git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "--allow-empty",
        "-m",
        "init",
        cwd=repo,
    )
    path, git_enabled = server._prepare_plain_repo(str(repo), False)
    assert git_enabled is True
    assert path == str(repo.resolve())
