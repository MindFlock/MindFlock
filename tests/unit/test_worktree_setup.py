"""O2/O3: per-worktree setup + verification-gate runners
(``backend.web.core.worktree_setup``).

Covers: ``.mindflock.toml`` config parsing, untracked-file propagation
(including escape rejection and never-overwrite), the setup/check background
runners with their status/log marker files, staleness vs HEAD, and the events
they emit. Runners execute real (trivial) shell commands in tmp dirs.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from backend.web.core import events as _events
from backend.web.core import worktree_setup as ws


def _wait(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def repo(tmp_path):
    """A tiny real git repo (for HEAD-sha staleness checks)."""
    r = tmp_path / "repo"
    r.mkdir()
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=r, check=True)
    (r / "a.txt").write_text("a")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)
    return r


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #
def test_load_config_missing_file_is_empty(tmp_path):
    cfg = ws.load_config(str(tmp_path))
    assert cfg.setup_commands is None
    assert cfg.copy_untracked == []
    assert cfg.check_command == ""
    assert not cfg.has_setup


def test_load_config_full(tmp_path):
    (tmp_path / ws.CONFIG_NAME).write_text(
        "[workspace]\n"
        'setup_commands = ["npm install", " "]\n'
        'copy_untracked = [".env", "  ", "conf/local.ini"]\n'
        'check_command = "npm test"\n'
    )
    cfg = ws.load_config(str(tmp_path))
    assert cfg.setup_commands == ["npm install"]
    assert cfg.copy_untracked == [".env", "conf/local.ini"]
    assert cfg.check_command == "npm test"
    assert cfg.has_setup


def test_load_config_malformed_toml_is_empty(tmp_path):
    (tmp_path / ws.CONFIG_NAME).write_text("[workspace\nnot toml")
    cfg = ws.load_config(str(tmp_path))
    assert not cfg.has_setup and cfg.check_command == ""


def test_load_config_wrong_types_ignored(tmp_path):
    (tmp_path / ws.CONFIG_NAME).write_text(
        "[workspace]\ncopy_untracked = 5\ncheck_command = 7\n"
    )
    cfg = ws.load_config(str(tmp_path))
    assert cfg.copy_untracked == [] and cfg.check_command == ""


# --------------------------------------------------------------------------- #
# copy_untracked
# --------------------------------------------------------------------------- #
def test_copy_untracked_files_and_dirs(tmp_path):
    repo = tmp_path / "r"
    wt = tmp_path / "w"
    (repo / "conf").mkdir(parents=True)
    wt.mkdir()
    (repo / ".env").write_text("SECRET=1")
    (repo / "conf" / "local.ini").write_text("[x]")
    copied = ws.copy_untracked(str(repo), str(wt), [".env", "conf", "missing"])
    assert copied == [".env", "conf"]
    assert (wt / ".env").read_text() == "SECRET=1"
    assert (wt / "conf" / "local.ini").exists()


def test_copy_untracked_never_overwrites(tmp_path):
    repo = tmp_path / "r"
    wt = tmp_path / "w"
    repo.mkdir()
    wt.mkdir()
    (repo / ".env").write_text("from-repo")
    (wt / ".env").write_text("mine")
    assert ws.copy_untracked(str(repo), str(wt), [".env"]) == []
    assert (wt / ".env").read_text() == "mine"


def test_copy_untracked_rejects_escapes(tmp_path):
    repo = tmp_path / "r"
    wt = tmp_path / "w"
    repo.mkdir()
    wt.mkdir()
    (tmp_path / "outside.txt").write_text("nope")
    copied = ws.copy_untracked(str(repo), str(wt), ["../outside.txt", "/etc/hostname"])
    assert copied == []
    assert not (wt / "outside.txt").exists()


# --------------------------------------------------------------------------- #
# Setup runner
# --------------------------------------------------------------------------- #
def test_setup_runner_ok_emits_and_records(tmp_path):
    repo = tmp_path / "r"
    wt = tmp_path / "w"
    repo.mkdir()
    wt.mkdir()
    (repo / ".env").write_text("K=1")
    cfg = ws.WorkspaceConfig(
        setup_commands=["echo hello", "touch made-it"], copy_untracked=[".env"]
    )
    got = []
    unsub = _events.BUS.subscribe(lambda env: got.append(env))
    try:
        assert ws.start_setup("t", str(repo), str(wt), cfg)
        assert _wait(lambda: (ws.setup_status(str(wt)) or {}).get("state") == "ok")
    finally:
        unsub()
    st = ws.setup_status(str(wt))
    assert st["rc"] == 0 and st["copied"] == [".env"]
    assert (wt / "made-it").exists()
    assert "hello" in ws.log_tail(str(wt), ws.SETUP_LOG)
    names = [e.get("event") for e in got]
    assert "session.setup_started" in names and "session.setup_finished" in names


def test_setup_runner_failure_state(tmp_path):
    repo = tmp_path / "r"
    wt = tmp_path / "w"
    repo.mkdir()
    wt.mkdir()
    cfg = ws.WorkspaceConfig(setup_commands=["exit 3"])
    assert ws.start_setup("t", str(repo), str(wt), cfg)
    assert _wait(lambda: (ws.setup_status(str(wt)) or {}).get("state") == "failed")
    assert ws.setup_status(str(wt))["rc"] == 3


def test_setup_noop_without_config(tmp_path):
    repo = tmp_path / "r"
    wt = tmp_path / "w"
    repo.mkdir()
    wt.mkdir()
    assert not ws.start_setup("t", str(repo), str(wt), ws.WorkspaceConfig())
    assert ws.setup_status(str(wt)) is None


def test_setup_runner_body_crash_marks_failed(tmp_path, monkeypatch):
    """A crash inside the runner body must drive the status to a terminal
    'failed' (with a log trace), never leave the card wedged at 'running'."""
    repo = tmp_path / "r"
    wt = tmp_path / "w"
    repo.mkdir()
    wt.mkdir()

    def boom(*_a, **_k):
        raise OSError("spawn failed")

    monkeypatch.setattr(ws, "_run_logged", boom)
    cfg = ws.WorkspaceConfig(setup_commands=["echo hi"])
    assert ws.start_setup("t", str(repo), str(wt), cfg)
    assert _wait(lambda: (ws.setup_status(str(wt)) or {}).get("state") == "failed")
    st = ws.setup_status(str(wt))
    assert st["state"] == "failed" and st["rc"] == 1
    assert not ws.is_running(str(wt), "setup")
    assert "runner crashed" in ws.log_tail(str(wt), ws.SETUP_LOG)


# --------------------------------------------------------------------------- #
# Check runner (verification gate)
# --------------------------------------------------------------------------- #
def test_check_records_head_sha_and_staleness(repo):
    assert ws.start_check("t", str(repo), "true")
    assert _wait(lambda: (ws.check_status(str(repo)) or {}).get("state") == "ok")
    st = ws.check_summary(str(repo))
    assert st["state"] == "ok" and st["sha"] and st["stale"] is False
    # A new commit makes the passing result stale.
    (repo / "b.txt").write_text("b")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "more"], cwd=repo, check=True)
    assert ws.check_summary(str(repo))["stale"] is True


def test_check_failure(repo):
    assert ws.start_check("t", str(repo), "exit 2")
    assert _wait(lambda: (ws.check_status(str(repo)) or {}).get("state") == "failed")
    assert ws.check_summary(str(repo))["rc"] == 2


def test_check_requires_command(tmp_path):
    assert not ws.start_check("t", str(tmp_path), "")
    assert not ws.start_check("t", str(tmp_path / "nope"), "true")


def test_check_runner_body_crash_marks_failed(tmp_path, monkeypatch):
    """The check runner must also reach a terminal 'failed' on a body crash."""
    wt = tmp_path / "w"
    wt.mkdir()

    def boom(*_a, **_k):
        raise OSError("spawn failed")

    monkeypatch.setattr(ws, "_run_logged", boom)
    assert ws.start_check("t", str(wt), "true")
    assert _wait(lambda: (ws.check_status(str(wt)) or {}).get("state") == "failed")
    assert ws.check_status(str(wt))["rc"] == 1
    assert not ws.is_running(str(wt), "check")
    assert "runner crashed" in ws.log_tail(str(wt), ws.CHECK_LOG)


def test_status_files_are_git_excluded():
    from backend.workspace_setup import WORKSPACE_ARTIFACTS

    for name in (ws.SETUP_LOG, ws.SETUP_STATUS, ws.CHECK_LOG, ws.CHECK_STATUS):
        assert name in WORKSPACE_ARTIFACTS


def test_status_json_roundtrip(tmp_path):
    ws._write_status(str(tmp_path), ws.SETUP_STATUS, {"state": "ok", "rc": 0})
    assert ws.setup_status(str(tmp_path)) == {"state": "ok", "rc": 0}
    (tmp_path / ws.CHECK_STATUS).write_text("not json")
    assert ws.check_status(str(tmp_path)) is None
