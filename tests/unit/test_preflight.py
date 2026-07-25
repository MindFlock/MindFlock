"""Phase-2 failure-path tests: dangerous repo states fail loudly and cleanly.

Every scenario a stranger can plausibly hit at session-create time must
(1) produce a message that names the problem and the exact fix, and
(2) leave the user's repo untouched (or cleanly rolled back).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from backend.session import preflight
from backend.session.git.worktree import GitWorktree
from backend.session.git.worktree_ops import _safe_rmtree


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "commit", "--allow-empty", "-q", "-m", "initial")
    return path


def _codes(issues, severity=None):
    return [i.code for i in issues if severity is None or i.severity == severity]


# --------------------------------------------------------------------------- #
# repo_issues classification
# --------------------------------------------------------------------------- #
def test_clean_repo_has_no_issues(tmp_path):
    repo = _init_repo(tmp_path / "r")
    assert preflight.repo_issues(str(repo)) == []


def test_dirty_working_tree_is_fine_by_design(tmp_path):
    # Worktrees fork the HEAD commit; uncommitted changes are NOT inherited,
    # so a dirty tree must not block (or even warn) — pin that contract.
    repo = _init_repo(tmp_path / "r")
    (repo / "uncommitted.txt").write_text("dirty")
    assert preflight.repo_issues(str(repo)) == []


def test_not_a_repo_blocks_with_fix(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    issues = preflight.repo_issues(str(d))
    assert _codes(issues, "block") == ["not-a-repo"]
    assert "git" in issues[0].fix


def test_missing_dir_blocks(tmp_path):
    issues = preflight.repo_issues(str(tmp_path / "nope"))
    assert _codes(issues, "block") == ["missing-dir"]


def test_no_commits_blocks_with_fix(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    issues = preflight.repo_issues(str(repo))
    assert _codes(issues, "block") == ["no-commits"]
    assert "--allow-empty" in issues[0].fix


def test_mid_rebase_blocks_with_abort_hint(tmp_path):
    repo = _init_repo(tmp_path / "r")
    (repo / ".git" / "rebase-merge").mkdir()
    issues = preflight.repo_issues(str(repo))
    assert "mid-rebase" in _codes(issues, "block")
    msg = preflight.blocking_error(str(repo))
    assert "rebase" in msg and "--abort" in msg


def test_mid_merge_blocks(tmp_path):
    repo = _init_repo(tmp_path / "r")
    (repo / ".git" / "MERGE_HEAD").write_text("0" * 40)
    assert "mid-merge" in _codes(preflight.repo_issues(str(repo)), "block")


def test_mid_cherry_pick_blocks(tmp_path):
    repo = _init_repo(tmp_path / "r")
    (repo / ".git" / "CHERRY_PICK_HEAD").write_text("0" * 40)
    assert "mid-cherry-pick" in _codes(preflight.repo_issues(str(repo)), "block")


def test_mid_bisect_blocks(tmp_path):
    repo = _init_repo(tmp_path / "r")
    (repo / ".git" / "BISECT_LOG").write_text("")
    assert "mid-bisect" in _codes(preflight.repo_issues(str(repo)), "block")


def test_detached_head_warns_but_does_not_block(tmp_path):
    repo = _init_repo(tmp_path / "r")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", sha)
    issues = preflight.repo_issues(str(repo))
    assert _codes(issues, "block") == []
    assert "detached-head" in _codes(issues, "warn")
    assert preflight.blocking_error(str(repo)) is None


def test_shallow_clone_warns_but_does_not_block(tmp_path):
    repo = _init_repo(tmp_path / "r")
    (repo / ".git" / "shallow").write_text("0" * 40)
    issues = preflight.repo_issues(str(repo))
    assert _codes(issues, "block") == []
    assert "shallow-clone" in _codes(issues, "warn")


def test_blocking_error_is_none_on_clean(tmp_path):
    repo = _init_repo(tmp_path / "r")
    assert preflight.blocking_error(str(repo)) is None


# --------------------------------------------------------------------------- #
# server-level wiring: session create 400s with the preflight message
# --------------------------------------------------------------------------- #
def test_prepare_plain_repo_raises_on_mid_rebase(tmp_path):
    from backend.web.server import _prepare_plain_repo

    repo = _init_repo(tmp_path / "r")
    (repo / ".git" / "rebase-merge").mkdir()
    with pytest.raises(ValueError, match="rebase"):
        _prepare_plain_repo(str(repo), False)
    # Nothing was written to the repo.
    assert (repo / ".git" / "rebase-merge").is_dir()


def test_prepare_plain_repo_ok_on_clean(tmp_path):
    from backend.web.server import _prepare_plain_repo

    repo = _init_repo(tmp_path / "r")
    path, git_enabled = _prepare_plain_repo(str(repo), False)
    assert git_enabled and os.path.samefile(path, repo)


# --------------------------------------------------------------------------- #
# ctrl-C during worktree creation: clean rollback, repo untouched, retryable
# --------------------------------------------------------------------------- #
def _worktree_for(repo: Path, tmp_path: Path, branch: str) -> GitWorktree:
    return GitWorktree(
        repoPath=str(repo),
        worktreePath=str(tmp_path / "wt" / "session_x"),
        sessionName="x",
        branchName=branch,
    )


def test_interrupt_mid_worktree_add_rolls_back(tmp_path, monkeypatch):
    """Simulate Ctrl-C landing right after `git worktree add` completed: the
    rollback must remove the worktree AND the just-created branch, leaving the
    repo exactly as it was — and the same title must be retryable."""
    repo = _init_repo(tmp_path / "r")
    wt = _worktree_for(repo, tmp_path, "session/x")
    orig = wt.run_git_command

    def interrupting(path, *args):
        out = orig(path, *args)
        if args[:2] == ("worktree", "add"):
            raise KeyboardInterrupt()
        return out

    monkeypatch.setattr(wt, "run_git_command", interrupting)
    with pytest.raises(KeyboardInterrupt):
        wt.setup_new_worktree()

    # Repo untouched: no leftover branch, no registered worktree, no dir.
    assert _git(repo, "show-ref", "--verify", "refs/heads/session/x").returncode != 0
    assert "session_x" not in _git(repo, "worktree", "list").stdout
    assert not Path(wt.worktreePath).exists()

    # Recoverable: the same session sets up cleanly afterwards.
    monkeypatch.setattr(wt, "run_git_command", orig)
    wt.setup_new_worktree()
    assert Path(wt.worktreePath).is_dir()
    assert _git(repo, "show-ref", "--verify", "refs/heads/session/x").returncode == 0
    wt.Cleanup()


def test_failed_worktree_add_raises_runtime_error_and_rolls_back(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "r")
    wt = _worktree_for(repo, tmp_path, "session/x")
    orig = wt.run_git_command

    def failing(path, *args):
        if args[:2] == ("worktree", "add"):
            raise RuntimeError("git command failed: disk full (exit status 128)")
        return orig(path, *args)

    monkeypatch.setattr(wt, "run_git_command", failing)
    with pytest.raises(RuntimeError, match="failed to create worktree"):
        wt.setup_new_worktree()
    assert _git(repo, "show-ref", "--verify", "refs/heads/session/x").returncode != 0
    assert not Path(wt.worktreePath).exists()


# --------------------------------------------------------------------------- #
# _safe_rmtree: never deletes the repo, its ancestors, home, or /
# --------------------------------------------------------------------------- #
def test_safe_rmtree_refuses_repo_and_ancestors(tmp_path):
    repo = _init_repo(tmp_path / "parent" / "repo")
    _safe_rmtree(str(repo), str(repo))
    assert repo.is_dir()
    _safe_rmtree(str(tmp_path / "parent"), str(repo))  # ancestor of the repo
    assert repo.is_dir()
    _safe_rmtree(str(Path.home()), str(repo))
    assert Path.home().is_dir()


def test_safe_rmtree_deletes_ordinary_worktree_dir(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    victim = tmp_path / "worktrees" / "session_y"
    victim.mkdir(parents=True)
    _safe_rmtree(str(victim), str(repo))
    assert not victim.exists()


# --------------------------------------------------------------------------- #
# doctor: version gates + per-distro fix commands
# --------------------------------------------------------------------------- #
def test_doctor_git_too_old_fails_with_fix(monkeypatch):
    from backend import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(doctor, "_run", lambda argv: (0, "git version 2.16.1"))
    c = doctor.check_git()
    assert c.status == "fail"
    assert "2.17" in c.detail and c.fix


def test_doctor_git_new_enough_ok(monkeypatch):
    from backend import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(doctor, "_run", lambda argv: (0, "git version 2.39.5"))
    assert doctor.check_git().status == "ok"


def test_doctor_tmux_too_old_fails_with_fix(monkeypatch):
    from backend import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(doctor, "_run", lambda argv: (0, "tmux 2.1"))
    c = doctor.check_tmux()
    assert c.status == "fail"
    assert "2.4" in c.detail and c.fix


def test_doctor_tmux_new_enough_ok(monkeypatch):
    from backend import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(doctor, "_run", lambda argv: (0, "tmux 3.4"))
    assert doctor.check_tmux().status == "ok"


def test_doctor_weird_version_string_degrades_to_ok(monkeypatch):
    # An unparseable version must not fail the check (never punish the user
    # for a distro's exotic version string).
    from backend import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(doctor, "_run", lambda argv: (0, "tmux next-3.6-rc"))
    # "next-3.6-rc" parses as 3.6 >= 2.4 -> ok; a fully unparseable string
    # yields () -> version gate skipped -> ok.
    assert doctor.check_tmux().status == "ok"
    monkeypatch.setattr(doctor, "_run", lambda argv: (0, "tmux master"))
    assert doctor.check_tmux().status == "ok"


@pytest.mark.parametrize(
    "available,expected",
    [
        ("apt", "sudo apt install tmux"),
        ("dnf", "sudo dnf install tmux"),
        ("pacman", "sudo pacman -S tmux"),
        ("zypper", "sudo zypper install tmux"),
    ],
)
def test_pkg_fix_matches_host_package_manager(monkeypatch, available, expected):
    from backend import doctor

    monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "linux")
    monkeypatch.setattr(
        doctor.shutil, "which", lambda b: "/usr/bin/" + b if b == available else None
    )
    assert doctor._pkg_fix("tmux") == expected


def test_pkg_fix_macos(monkeypatch):
    from backend import doctor

    monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
    assert doctor._pkg_fix("tmux") == "brew install tmux"


def test_doctor_clipboard_linux_missing_is_info_not_fail(monkeypatch):
    # Optional dep: absence must never fail the doctor, but must carry a fix.
    from backend import doctor

    monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "linux")
    monkeypatch.setattr(doctor.shutil, "which", lambda b: None)
    c = doctor.check_clipboard()
    assert c.status == "info"
    assert "xclip" in c.fix


def test_doctor_clipboard_nonlinux_ok(monkeypatch):
    from backend import doctor

    for kind in ("macos", "wsl"):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda k=kind: k)
        assert doctor.check_clipboard().status == "ok"
