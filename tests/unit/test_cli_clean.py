"""``mindflock clean`` (backend/cli_clean.py), run in-process with ``--local``
against a throwaway home and repository: the listing is a dry run,
``--collect`` removes the safe set, ``--remove`` of an item that holds work
asks first and refuses without a terminal, and ``--yes`` saves the work."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from backend import cli
from backend.web.core import worktree_gc as gc
from backend.web.core import worktree_gc_driver as drv

DAY = 24 * 3600.0


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


def _age(path, days=60):
    old = time.time() - days * DAY
    os.utime(path, (old, old))
    gd = gc.gitdir_of(path)
    for n in ("HEAD", "index", os.path.join("logs", "HEAD")):
        p = os.path.join(gd, n)
        if os.path.exists(p):
            os.utime(p, (old, old))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text("[user]\n\tname = t\n\temail = t@example.com\n")
    monkeypatch.setenv("MINDFLOCK_HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gcfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(drv, "_clone_roots", lambda: [])
    drv._report.clear()
    # A removal closes editor windows on the folder (xdotool, by title):
    # never let a test reach the real desktop.
    from backend.web.core import cursor_windows

    monkeypatch.setattr(cursor_windows, "_close_cursor_window", lambda path: None)
    gc._size_cache.clear()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("1")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "c")
    root = home / "worktrees" / "u"
    safe, wip = root / "safe", root / "wip"
    _git(repo, "worktree", "add", "-q", "-b", "u/safe", str(safe))
    _git(repo, "worktree", "add", "-q", "-b", "u/wip", str(wip))
    (wip / "draft.txt").write_text("unsaved idea\n")
    _age(str(safe))
    _age(str(wip))
    return {"home": home, "safe": safe, "wip": wip}


def test_the_listing_is_a_dry_run(setup, capsys):
    assert cli.main(["clean", "--local"]) == 0
    out = capsys.readouterr().out
    assert "remove" in out and "u/safe" in out
    assert "1 untracked file" in out
    assert "1 can be removed safely" in out and "mindflock clean --collect" in out
    assert setup["safe"].is_dir() and setup["wip"].is_dir()


def test_collect_removes_only_the_safe_set(setup, capsys):
    assert cli.main(["clean", "--local", "--collect"]) == 0
    out = capsys.readouterr().out
    assert "1 removed" in out
    assert not setup["safe"].exists() and setup["wip"].is_dir()


def test_remove_of_work_asks_and_refuses_without_a_terminal(setup, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli.main(["clean", "--local", "--remove", "u/wip"]) == 1
    out = capsys.readouterr().out
    assert "1 untracked file" in out and "Nothing removed." in out
    assert setup["wip"].is_dir()


def test_remove_yes_saves_the_work_and_says_where(setup, capsys):
    assert cli.main(["clean", "--local", "--remove", "u/wip", "--yes"]) == 0
    out = capsys.readouterr().out
    assert not setup["wip"].exists()
    line = next(
        ln for ln in out.splitlines() if ln.startswith("uncommitted changes saved: ")
    )
    patch = line.split(": ", 1)[1]
    assert patch.startswith(str(setup["home"] / "kept-work"))
    assert "unsaved idea" in open(patch).read()


def test_mode_and_days_are_set(setup, capsys):
    assert cli.main(["clean", "--local", "--mode", "auto", "--after-days", "30"]) == 0
    assert "cleanup: auto, after 30 days unused" in capsys.readouterr().out
    assert drv.settings() == {"mode": "auto", "after_days": 30}
