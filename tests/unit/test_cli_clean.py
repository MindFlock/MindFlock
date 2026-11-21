"""``mindflock clean`` (backend/cli_clean.py), run in-process with ``--local``
against a throwaway home and repository: the listing is a dry run,
``--collect`` removes the safe set, ``--remove`` of an item that holds work
asks first and refuses without a terminal, and ``--yes`` saves the work."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from backend import cli, cli_clean
from backend.web.core import worktree_gc as gc
from backend.web.core import worktree_gc_driver as drv

DAY = 24 * 3600.0


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


def _old_git(monkeypatch, days=60):
    """Make every commit and reflog entry from here on ``days`` old: the
    collector's clock is the worktree's HEAD reflog, not file mtimes."""
    stamp = "@%d +0000" % int(time.time() - days * DAY)
    monkeypatch.setenv("GIT_COMMITTER_DATE", stamp)
    monkeypatch.setenv("GIT_AUTHOR_DATE", stamp)


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
    gc._caches.clear()
    _old_git(monkeypatch)
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


# ------------------------------------------------------- a listing that talks


def test_the_scan_publishes_the_list_before_the_sizes(setup):
    seen = []

    def on_listed(rep):
        # What the screen (and the CLI) gets while the walks still run.
        seen.append([(i["state"], i["size_bytes"]) for i in rep["items"]])
        assert rep["sizing"] is True
        assert drv.report()["items"] is not None  # GET /api/cleanup has it too

    final = drv.scan(None, fetch=False, on_listed=on_listed)
    assert seen and all(size is None for _, size in seen[0])
    assert final["sizing"] is False
    assert all(i["size_bytes"] for i in final["items"])
    # Next time the sizes come from the cache: nothing left to walk.
    seen.clear()
    again = drv.scan(
        None, fetch=False, on_listed=lambda rep: seen.append(rep["sizing"])
    )
    assert seen == [False] and again["summary"] == final["summary"]


def test_json_stays_clean_while_progress_goes_to_a_terminal(setup, capsys, monkeypatch):
    monkeypatch.setattr("sys.stderr.isatty", lambda: True, raising=False)
    assert cli.main(["clean", "--local", "--json"]) == 0
    cap = capsys.readouterr()
    rep = json.loads(cap.out)  # stdout is the report and nothing else
    assert rep["sizing"] is False and rep["summary"]["bytes"] > 0
    assert all(i["size_bytes"] is not None for i in rep["items"])
    assert "checking…" in cap.err and "measuring sizes…" in cap.err


def test_no_progress_on_a_pipe(setup, capsys):
    assert cli.main(["clean", "--local"]) == 0
    assert capsys.readouterr().err == ""


def test_the_table_comes_first_on_a_terminal_then_the_sizes(setup, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    assert cli.main(["clean", "--local"]) == 0
    out = capsys.readouterr().out
    table, rest = out.split("Sizes measured since:", 1)
    assert "u/safe" in table and "1 untracked file" in table
    assert "u/safe" in rest and "1 can be removed safely" in rest
    # Warm: one table with its sizes, nothing measured after it.
    assert cli.main(["clean", "--local"]) == 0
    assert "Sizes measured since:" not in capsys.readouterr().out


def test_no_size_lists_at_once_with_what_is_known(setup, capsys):
    assert cli.main(["clean", "--local", "--no-size"]) == 0
    out = capsys.readouterr().out
    assert "1 can be removed safely" in out
    assert "isn't fully measured; without --no-size it is" in out
    assert cli.main(["clean", "--local"]) == 0  # measures (and caches)
    capsys.readouterr()
    assert cli.main(["clean", "--local", "--no-size"]) == 0
    out = capsys.readouterr().out
    assert "isn't fully measured" not in out and " on disk." in out


def test_no_fetch_is_passed_through(setup, monkeypatch, capsys):
    seen = []
    real = drv.scan
    monkeypatch.setattr(
        drv, "scan", lambda *a, **k: seen.append(k.get("fetch")) or real(*a, **k)
    )
    assert cli.main(["clean", "--local", "--no-fetch"]) == 0
    assert cli.main(["clean", "--local"]) == 0
    assert seen == [False, True]


def test_a_stale_base_is_said_under_the_listing(capsys):
    rep = {
        "items": [],
        "summary": {"count": 0, "bytes": 0, "safe_count": 0},
        "bases": [
            {
                "repo": "/r/_base_x",
                "names": ["staging"],
                "status": "failed",
                "error": "Could not read from remote repository.",
                "last_fetched": time.time() - 25 * 3600,
                "stale": True,
            },
            {"repo": "/r/y", "names": ["main"], "status": "fetched", "stale": False},
        ],
    }
    out = cli_clean.render(rep)
    assert (
        "origin/staging of /r/_base_x: last fetched 25 hours ago, couldn't be "
        "refreshed: Could not read from remote repository." in out
    )
    assert "Base branches fetched just now for 1 repository." in out


def test_through_the_server_the_list_comes_before_the_sizes(monkeypatch):
    """`mindflock clean` against a running server: it asks for a fresh scan
    (passing --no-fetch / --no-size on), shows the list on the first report
    that has it while sizes are measured, and returns the finished one."""
    item = {"id": "a", "state": "safe", "size_bytes": None}
    replies = [
        {"scanning": True, "items": None, "progress": {"phase": "check", "done": 1, "total": 2}},
        {"scanning": True, "sizing": True, "items": [item]},
        {"scanning": True, "sizing": True, "items": [item],
         "progress": {"phase": "size", "done": 0, "total": 1}},
        {"scanning": False, "sizing": False, "items": [dict(item, size_bytes=5)]},
    ]  # fmt: skip
    asked = []

    def get(base, path):
        asked.append(path)
        return replies.pop(0)

    monkeypatch.setattr(cli_clean.client, "get", get)
    monkeypatch.setattr(cli_clean.time, "sleep", lambda s: None)
    listed, steps = [], []
    rep = cli_clean._Server("http://x").report(
        fetch=False,
        on_listed=lambda r: listed.append([i["size_bytes"] for i in r["items"]]),
        progress=lambda *a: steps.append(a),
    )
    assert asked[0] == "/api/cleanup?refresh=1&fetch=0"
    assert listed == [[None]]  # once, before the sizes
    assert steps == [("check", 1, 2), ("size", 0, 1)]
    assert rep["items"][0]["size_bytes"] == 5
