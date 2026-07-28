"""Unit tests for :mod:`backend.web.core.snapshot`.

The pure parsing/labelling helpers behind the sidebar descriptors:
``_parse_shortstat`` (git diff --shortstat → counts), ``_folder_label`` (short
folder label, worktree-aware), and ``_repo_name`` fallbacks. The git-driven
diff-stat path is left to the integration layer; these lock the string
handling that has no I/O.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from backend import config
from backend.web.core import snapshot


# --------------------------------------------------------------------------- #
# _parse_shortstat
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "out,expected",
    [
        ("", {"files": 0, "additions": 0, "deletions": 0}),
        (
            " 3 files changed, 42 insertions(+), 7 deletions(-)",
            {"files": 3, "additions": 42, "deletions": 7},
        ),
        (
            " 1 file changed, 5 insertions(+)",
            {"files": 1, "additions": 5, "deletions": 0},
        ),
        (
            " 2 files changed, 9 deletions(-)",
            {"files": 2, "additions": 0, "deletions": 9},
        ),
        (
            " 1 file changed, 1 insertion(+), 1 deletion(-)",
            {"files": 1, "additions": 1, "deletions": 1},
        ),
    ],
)
def test_parse_shortstat(out, expected):
    assert snapshot._parse_shortstat(out) == expected


# --------------------------------------------------------------------------- #
# _folder_label
# --------------------------------------------------------------------------- #
def test_folder_label_blank():
    assert snapshot._folder_label("") == ""


def test_folder_label_plain_dir_uses_basename(tmp_path):
    d = tmp_path / "my-project"
    d.mkdir()
    assert snapshot._folder_label(str(d)) == "my-project"


def test_folder_label_worktree_shows_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GetConfigDir", lambda: str(tmp_path))
    wt_root = tmp_path / "worktrees"
    leaf = wt_root / "mindflock" / "gamer3_a1b2c3"
    leaf.mkdir(parents=True)
    # A worktree nested under worktrees/<repo>/ shows the repo dir (its parent).
    assert snapshot._folder_label(str(leaf)) == "mindflock"


def test_folder_label_worktree_directly_under_root_strips_hex(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GetConfigDir", lambda: str(tmp_path))
    wt_root = tmp_path / "worktrees"
    leaf = wt_root / "myrepo_deadbeef"
    leaf.mkdir(parents=True)
    # Sits directly under worktrees/ → leaf basename with the _<hex> stripped.
    assert snapshot._folder_label(str(leaf)) == "myrepo"


# --------------------------------------------------------------------------- #
# _repo_name
# --------------------------------------------------------------------------- #
def test_repo_name_root_or_missing_returns_blank():
    inst = SimpleNamespace(Provisioned=False, Path=".")
    inst.GetGitWorktree = lambda: (_ for _ in ()).throw(RuntimeError("no worktree"))
    assert snapshot._repo_name(inst) == ""


def test_repo_name_falls_back_to_path_basename():
    inst = SimpleNamespace(Provisioned=False, Path="/home/u/projects/coolrepo")
    inst.GetGitWorktree = lambda: (_ for _ in ()).throw(RuntimeError("no worktree"))
    assert snapshot._repo_name(inst) == "coolrepo"


def test_repo_name_provisioned_prefers_local_provision_repo():
    inst = SimpleNamespace(
        Provisioned=True,
        _provision_repo="/srv/clones/target-repo",
        _workspace_path="",
        Path="/whatever",
    )
    assert snapshot._repo_name(inst) == "target-repo"
