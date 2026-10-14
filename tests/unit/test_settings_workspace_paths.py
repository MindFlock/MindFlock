"""Settings → Workspace: "where do my worktrees live?" (GET
/api/settings/workspace-paths) — both locations as absolute paths, a typed
folder checked for existence/writability, and a typed ``~/…`` stored expanded
(the ticket pipeline reads the field as a plain path)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.web.addons import settings as settings_addon


@pytest.fixture
def roots(monkeypatch, tmp_path):
    cfg = tmp_path / "cfg"
    repo = tmp_path / "repo"
    cfg.mkdir()
    repo.mkdir()
    monkeypatch.setenv("MINDFLOCK_REPO_ROOT", str(repo))
    monkeypatch.delenv("MINDFLOCK_WORKSPACE_DIR", raising=False)
    monkeypatch.setattr(
        "backend.session.git.worktree.get_worktree_directory",
        lambda: str(cfg / "worktrees"),
    )
    return cfg, repo


def test_sessions_you_start_live_under_the_config_dir(roots):
    cfg, _ = roots
    out = settings_addon.workspace_paths("")
    assert out["sessions_dir"] == str(cfg / "worktrees")


def test_blank_folder_is_the_default_anchored_at_the_pipeline_root(roots):
    _, repo = roots
    out = settings_addon.workspace_paths("")
    assert out["tickets_dir"] == str(repo / "workspaces")
    assert out["tickets_default"] is True
    assert os.path.isabs(out["tickets_dir"])


def test_tilde_is_expanded_and_a_missing_folder_will_be_created(
    roots, monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = settings_addon.workspace_paths("~/nowhere/yet")
    assert out["tickets_dir"] == str(tmp_path / "nowhere" / "yet")
    assert out["tickets_state"] == "create"
    assert "creates it" in out["tickets_warning"]


def test_existing_writable_folder_is_ok(roots, tmp_path):
    out = settings_addon.workspace_paths(str(tmp_path))
    assert out["tickets_state"] == "ok" and out["tickets_warning"] == ""


def test_a_file_is_not_a_folder(roots, tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    out = settings_addon.workspace_paths(str(f))
    assert out["tickets_state"] == "not_dir"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_an_unwritable_parent_is_warned(roots, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        out = settings_addon.workspace_paths(str(locked / "ws"))
        assert out["tickets_state"] == "not_writable"
        assert "can't create it" in out["tickets_warning"]
    finally:
        locked.chmod(0o700)


def test_env_override_wins_and_is_named(roots, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_WORKSPACE_DIR", str(tmp_path / "envws"))
    out = settings_addon.workspace_paths("/typed/elsewhere")
    assert out["tickets_dir"] == str(tmp_path / "envws")
    assert out["env_override"] == "MINDFLOCK_WORKSPACE_DIR"


def test_saved_value_is_used_when_nothing_is_typed(roots, monkeypatch, tmp_path):
    class _Repo:
        workspace_dir = str(tmp_path / "saved")

    class _S:
        repository = _Repo()

    monkeypatch.setattr(settings_addon.settings_store, "load_settings", lambda: _S())
    out = settings_addon.workspace_paths(None)
    assert out["tickets_dir"] == str(tmp_path / "saved")
    assert out["tickets_default"] is False


def test_a_typed_tilde_folder_is_stored_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    got = settings_addon._expand_workspace_dir(
        {"repository": {"workspace_dir": " ~/mf/ws ", "base_branch": "main"}}
    )
    assert got["repository"] == {
        "workspace_dir": str(Path(tmp_path) / "mf" / "ws"),
        "base_branch": "main",
    }
    # Anything else passes through untouched.
    same = {"repository": {"workspace_dir": "./workspaces"}}
    assert settings_addon._expand_workspace_dir(same) is same
    assert settings_addon._expand_workspace_dir({"general": {}}) == {"general": {}}


def test_route_is_registered():
    from backend.web import server

    assert "/api/settings/workspace-paths" in server.app.openapi()["paths"]
