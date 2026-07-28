"""Hermetic tests for :mod:`backend.uninstall` (``mindflock uninstall``).

Every test redirects ``$HOME`` at ``tmp_path``, so ``GetConfigDir()`` and the
purge guard resolve to throwaway directories and the real ``~/.mindflock`` is
never touched. The worktree tests drive a real ``git`` — that's the whole point
of the command (leaving a repo with registered worktrees pointing at deleted
paths is the bug it exists to prevent), so faking git would test nothing.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from backend import uninstall
from backend.providers import activity_markers as am

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is required for the worktree teardown tests",
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point $HOME (and therefore ~/.mindflock) at a throwaway directory."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _git(repo, *args):
    cp = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A git repo with one commit, ready to hang worktrees off."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "README.md").write_text("hi\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def _write_state(home, instances):
    d = home / ".mindflock"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(
        json.dumps({"help_screens_seen": 0, "instances": instances})
    )


def _mf_hook_entry(cmd="echo hi"):
    """A hook entry shaped like ours, carrying the MindFlock tag."""
    return {"hooks": [{"type": "command", "command": cmd + " # mindflock-activity"}]}


def _user_hook_entry(cmd="make lint"):
    return {"hooks": [{"type": "command", "command": cmd}]}


# --------------------------------------------------------------------------- #
# Hook removal (the regrowth bug)
# --------------------------------------------------------------------------- #
def test_remove_activity_hooks_keeps_user_hooks(tmp_path):
    p = tmp_path / "settings.local.json"
    p.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash"]},
                "hooks": {
                    "Stop": [_mf_hook_entry(), _user_hook_entry()],
                    "PreToolUse": [_mf_hook_entry()],
                },
            }
        )
    )

    assert am.remove_activity_hooks(p) is True

    data = json.loads(p.read_text())
    # The user's own hook and unrelated keys survive verbatim…
    assert data["permissions"] == {"allow": ["Bash"]}
    assert data["hooks"]["Stop"] == [_user_hook_entry()]
    # …and an event left with nothing is dropped rather than left empty.
    assert "PreToolUse" not in data["hooks"]


def test_remove_activity_hooks_deletes_a_file_that_was_only_ours(tmp_path):
    """The common case: a settings file MindFlock created to hold its hooks."""
    p = tmp_path / "settings.local.json"
    p.write_text(json.dumps({"hooks": {"Stop": [_mf_hook_entry()]}}))

    assert am.remove_activity_hooks(p) is True
    assert not p.exists()


def test_remove_activity_hooks_keeps_a_file_with_other_keys(tmp_path):
    p = tmp_path / "settings.local.json"
    p.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [_mf_hook_entry()]}}))

    assert am.remove_activity_hooks(p) is True
    assert p.exists()
    assert json.loads(p.read_text()) == {"model": "opus", "hooks": {}}


def test_remove_activity_hooks_is_a_noop_without_our_entries(tmp_path):
    p = tmp_path / "settings.local.json"
    original = json.dumps({"hooks": {"Stop": [_user_hook_entry()]}})
    p.write_text(original)

    assert am.remove_activity_hooks(p) is False
    assert p.read_text() == original


@pytest.mark.parametrize("body", ["not json at all", "[]", '"a string"'])
def test_remove_activity_hooks_survives_junk(tmp_path, body):
    p = tmp_path / "settings.local.json"
    p.write_text(body)
    assert am.remove_activity_hooks(p) is False
    assert p.exists()


def test_remove_activity_hooks_missing_file(tmp_path):
    assert am.remove_activity_hooks(tmp_path / "nope.json") is False


# --------------------------------------------------------------------------- #
# .git/info/exclude
# --------------------------------------------------------------------------- #
def test_remove_git_exclude_removes_only_exact_lines(repo):
    am.ensure_git_excluded(str(repo), ".claude/settings.local.json")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "my/.claude/settings.local.json.bak\n")

    assert am.remove_git_exclude(str(repo), ".claude/settings.local.json") is True

    lines = exclude.read_text().splitlines()
    assert ".claude/settings.local.json" not in lines
    # A user pattern that merely CONTAINS the string is untouched.
    assert "my/.claude/settings.local.json.bak" in lines


def test_remove_git_exclude_noop_when_absent(repo):
    assert am.remove_git_exclude(str(repo), ".claude/settings.local.json") is False


def test_remove_git_exclude_outside_a_repo(tmp_path):
    assert am.remove_git_exclude(str(tmp_path), "whatever") is False


# --------------------------------------------------------------------------- #
# clean_workdir
# --------------------------------------------------------------------------- #
def test_clean_workdir_removes_hooks_and_artifacts(repo):
    settings = repo / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"Stop": [_mf_hook_entry()]}}))
    am.ensure_git_excluded(str(repo), ".claude/settings.local.json")
    (repo / ".mindflock_prompt.md").write_text("seed")
    (repo / ".mindflock_pastes").mkdir()
    (repo / ".mindflock_pastes" / "img.png").write_bytes(b"x")
    keep = repo / "README.md"

    report = uninstall.Report()
    uninstall.clean_workdir(str(repo), report)

    assert not settings.exists()
    assert not (repo / ".mindflock_prompt.md").exists()
    assert not (repo / ".mindflock_pastes").exists()
    assert keep.exists(), "clean_workdir must never touch the user's files"
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".claude/settings.local.json" not in exclude
    assert not report.errors


def test_clean_workdir_dry_run_changes_nothing(repo):
    settings = repo / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"Stop": [_mf_hook_entry()]}}))
    (repo / ".mindflock_prompt.md").write_text("seed")

    report = uninstall.Report()
    uninstall.clean_workdir(str(repo), report, dry_run=True)

    assert settings.exists()
    assert (repo / ".mindflock_prompt.md").exists()
    assert any("would strip" in a for a in report.actions)
    assert any("would remove" in a for a in report.actions)


def test_clean_workdir_dry_run_ignores_user_only_hook_files(repo):
    """--dry-run must not claim it will strip a file holding no hooks of ours."""
    settings = repo / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"Stop": [_user_hook_entry()]}}))

    report = uninstall.Report()
    uninstall.clean_workdir(str(repo), report, dry_run=True)

    assert not any("would strip" in a for a in report.actions)


# --------------------------------------------------------------------------- #
# Worktree teardown — the "stale registration" bug
# --------------------------------------------------------------------------- #
def _add_worktree(repo, home, name="wt", branch="mf/session"):
    wt = home / ".mindflock" / "worktrees" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "-b", branch, str(wt))
    return wt


def _registered(repo):
    out = _git(repo, "worktree", "list", "--porcelain")
    return [
        ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")
    ]


def test_removes_worktree_branch_and_registration(repo, home):
    wt = _add_worktree(repo, home)
    assert len(_registered(repo)) == 2

    target = uninstall.SessionTarget(
        title="s",
        repo_path=str(repo),
        worktree_path=str(wt),
        branch="mf/session",
    )
    report = uninstall.Report()
    uninstall.remove_session_worktree(target, report)

    assert not wt.exists()
    # The registration is gone — this is what a bare `rm -rf ~/.mindflock` leaves behind.
    assert len(_registered(repo)) == 1
    assert "mf/session" not in _git(repo, "branch", "--list", "mf/session")
    assert not report.errors


def test_keeps_a_pre_existing_branch(repo, home):
    _git(repo, "branch", "mine")
    wt = home / ".mindflock" / "worktrees" / "wt"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "mine")

    target = uninstall.SessionTarget(
        title="s",
        repo_path=str(repo),
        worktree_path=str(wt),
        branch="mine",
        is_existing_branch=True,
    )
    uninstall.remove_session_worktree(target, uninstall.Report())

    assert not wt.exists()
    assert "mine" in _git(repo, "branch", "--list", "mine")


def test_refuses_a_worktree_outside_the_mindflock_dir(repo, home, tmp_path):
    """MindFlock didn't create it, so uninstall must not remove it."""
    elsewhere = tmp_path / "user-worktree"
    _git(repo, "worktree", "add", "-q", "-b", "user-branch", str(elsewhere))

    target = uninstall.SessionTarget(
        title="s",
        repo_path=str(repo),
        worktree_path=str(elsewhere),
        branch="user-branch",
    )
    assert target.removable_worktree is False

    report = uninstall.Report()
    uninstall.remove_session_worktree(target, report)

    assert elsewhere.exists()
    assert "user-branch" in _git(repo, "branch", "--list", "user-branch")
    assert any("left worktree" in a for a in report.actions)


def test_in_place_session_worktree_is_never_removed(repo, home):
    target = uninstall.SessionTarget(
        title="s",
        repo_path=str(repo),
        worktree_path=str(repo),
        branch="main",
        in_place=True,
    )
    assert target.removable_worktree is False
    uninstall.remove_session_worktree(target, uninstall.Report())
    assert (repo / "README.md").exists()


def test_worktree_dry_run_changes_nothing(repo, home):
    wt = _add_worktree(repo, home)
    target = uninstall.SessionTarget(
        title="s", repo_path=str(repo), worktree_path=str(wt), branch="mf/session"
    )
    report = uninstall.Report()
    uninstall.remove_session_worktree(target, report, dry_run=True)

    assert wt.exists()
    assert len(_registered(repo)) == 2
    assert any("would remove worktree" in a for a in report.actions)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def test_build_plan_reads_state(repo, home):
    wt = _add_worktree(repo, home)
    _write_state(
        home,
        [
            {
                "title": "s1",
                "path": str(wt),
                "in_place": False,
                "worktree": {
                    "repo_path": str(repo),
                    "worktree_path": str(wt),
                    "branch_name": "mf/session",
                    "is_existing_branch": False,
                },
            }
        ],
    )

    plan = uninstall.build_plan()

    assert len(plan.sessions) == 1
    assert plan.sessions[0].removable_worktree is True
    assert str(repo) in plan.workdirs and str(wt) in plan.workdirs
    assert plan.orphan_worktrees == []
    assert not plan.warnings


def test_build_plan_finds_orphan_worktrees(repo, home):
    orphan = home / ".mindflock" / "worktrees" / "forgotten"
    orphan.mkdir(parents=True)
    _write_state(home, [])

    plan = uninstall.build_plan()

    assert plan.orphan_worktrees == [str(orphan)]


def test_build_plan_warns_on_unreadable_state(home):
    d = home / ".mindflock"
    d.mkdir(parents=True)
    (d / "state.json").write_text("{ this is not json")

    plan = uninstall.build_plan()

    assert plan.sessions == []
    assert any("could not read" in w for w in plan.warnings)


def test_build_plan_tolerates_a_missing_state_file(home):
    plan = uninstall.build_plan()
    assert plan.sessions == []
    assert not plan.warnings


def test_build_plan_handles_a_newer_schema_without_moving_it(home):
    """Uninstall must read a downgrade-scenario state file, not consume it."""
    d = home / ".mindflock"
    d.mkdir(parents=True)
    state = d / "state.json"
    state.write_text(json.dumps({"schema_version": 99, "instances": []}))

    plan = uninstall.build_plan()

    assert state.exists(), "build_plan must not move the state file aside"
    assert not plan.warnings


# --------------------------------------------------------------------------- #
# Purge guard
# --------------------------------------------------------------------------- #
def test_purge_removes_the_home_dirs(home):
    (home / ".mindflock").mkdir()
    (home / ".mindflock" / "state.json").write_text("{}")
    (home / ".mindflock-assistant").mkdir()

    plan = uninstall.build_plan()
    report = uninstall.execute(plan, purge=True)

    assert not (home / ".mindflock").exists()
    assert not (home / ".mindflock-assistant").exists()
    assert not report.errors


def test_execute_without_purge_keeps_the_home_dirs(home):
    (home / ".mindflock").mkdir()
    (home / ".mindflock-assistant").mkdir()

    uninstall.execute(uninstall.build_plan(), purge=False)

    assert (home / ".mindflock").exists()
    assert (home / ".mindflock-assistant").exists()


def test_purge_refuses_anything_that_is_not_a_mindflock_dir(home, tmp_path):
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "data.txt").write_text("keep me")

    report = uninstall.Report()
    uninstall._purge_dir(str(victim), report)

    assert victim.exists()
    assert (victim / "data.txt").exists()
    assert any("refused to purge" in e for e in report.errors)


def test_purge_refuses_the_home_directory_itself(home):
    report = uninstall.Report()
    uninstall._purge_dir(str(home), report)

    assert home.exists()
    assert any("refused to purge" in e for e in report.errors)


def test_purge_dry_run_changes_nothing(home):
    (home / ".mindflock").mkdir()
    report = uninstall.Report()
    uninstall._purge_dir(str(home / ".mindflock"), report, dry_run=True)

    assert (home / ".mindflock").exists()
    assert any("would delete" in a for a in report.actions)


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #
def test_execute_full_run(repo, home):
    """One pass leaves the repo consistent and MindFlock-free."""
    wt = _add_worktree(repo, home)
    for d in (repo, wt):
        s = d / ".claude" / "settings.local.json"
        s.parent.mkdir(exist_ok=True)
        s.write_text(json.dumps({"hooks": {"Stop": [_mf_hook_entry()]}}))
    (repo / ".mindflock_prompt.md").write_text("seed")
    _write_state(
        home,
        [
            {
                "title": "s1",
                "path": str(wt),
                "in_place": False,
                "worktree": {
                    "repo_path": str(repo),
                    "worktree_path": str(wt),
                    "branch_name": "mf/session",
                    "is_existing_branch": False,
                },
            }
        ],
    )

    report = uninstall.execute(uninstall.build_plan(), purge=True)

    assert not report.errors, report.errors
    assert not wt.exists()
    assert len(_registered(repo)) == 1
    assert not (repo / ".claude" / "settings.local.json").exists()
    assert not (repo / ".mindflock_prompt.md").exists()
    assert not (home / ".mindflock").exists()
    assert (repo / "README.md").exists()


def test_keep_worktrees_still_cleans_hooks(repo, home):
    wt = _add_worktree(repo, home)
    s = repo / ".claude" / "settings.local.json"
    s.parent.mkdir()
    s.write_text(json.dumps({"hooks": {"Stop": [_mf_hook_entry()]}}))
    _write_state(
        home,
        [
            {
                "title": "s1",
                "path": str(wt),
                "in_place": False,
                "worktree": {
                    "repo_path": str(repo),
                    "worktree_path": str(wt),
                    "branch_name": "mf/session",
                },
            }
        ],
    )

    uninstall.execute(uninstall.build_plan(), keep_worktrees=True)

    assert wt.exists()
    assert not s.exists()


def test_orphan_scan_failure_warns_instead_of_reporting_zero(home, monkeypatch):
    """An empty list reads as 'nothing to clean' — a failed scan must not."""
    monkeypatch.setattr(
        uninstall,
        "_orphan_worktrees",
        lambda known: ([], "could not scan /x (boom) — orphaned worktrees not checked"),
    )

    plan = uninstall.build_plan()

    assert plan.orphan_worktrees == []
    assert any("could not scan" in w for w in plan.warnings)


def test_missing_worktrees_dir_is_not_a_warning(home):
    plan = uninstall.build_plan()
    assert plan.orphan_worktrees == []
    assert not plan.warnings
