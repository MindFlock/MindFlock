"""``ship_status`` carries the failed hook's own output (``failed_output``) —
the same section Fast-track's failure note quotes — after a failed commit."""

import subprocess

import pytest

from backend.web import server
from backend.web.core import agent_state
from backend.web.core import ship_status

PANE = (
    "$ git commit -F .mindflock_commit_msg\n"
    "black....Failed\n- hook id: black\n- files were modified by this hook\n\n"
    "reformatted app.py\n1 file reformatted.\n"
    "flake8....Passed\n$ \n"
)


@pytest.fixture
def wt(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr(agent_state, "_precommit_lock_active", lambda i, w: False)
    monkeypatch.setattr(server, "_has_origin", lambda w, *a: True)
    monkeypatch.setattr(server, "_current_branch", lambda w: "feat/x")
    monkeypatch.setattr(server, "_git_head_sha", lambda w: "")
    monkeypatch.setattr(server, "_session_base_branch", lambda inst: "")
    monkeypatch.setattr(server, "_is_dirty", lambda w: True)
    monkeypatch.setattr(server, "_failed_precommit_step", lambda t: "black")
    monkeypatch.setattr(server, "_failed_precommit_hook", lambda t: "black")
    captured = []

    def capture(title, lines=400):
        captured.append(title)
        return PANE

    monkeypatch.setattr(server, "_capture_shell_pane", capture)
    return tmp_path, captured


def test_a_failed_commit_carries_the_hooks_output(wt):
    path, captured = wt
    (path / server._COMMIT_STATUS_FILE).write_text("1\n")
    doc = ship_status.snapshot(object(), "s", str(path), tail=2)
    assert doc["failed_output"] == (
        "- hook id: black\n- files were modified by this hook\n"
        "reformatted app.py\n1 file reformatted."
    )
    assert doc["shell_tail"] == "flake8....Passed\n$ "
    assert captured == ["s"], "one pane capture serves both"


def test_no_failure_no_output(wt):
    path, captured = wt
    doc = ship_status.snapshot(object(), "s", str(path))
    assert doc["failed_output"] is None and captured == []
    (path / server._COMMIT_STATUS_FILE).write_text("0\n")
    assert ship_status.snapshot(object(), "s", str(path))["failed_output"] is None
