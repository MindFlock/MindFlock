"""The worktree garbage collector's server side
(backend/web/core/worktree_gc_driver.py + routes/cleanup.py): who uses what,
the setting, the once-only notice, the pass per mode, and the routes' auth.
Every git repository here is a throwaway one; the routes are driven with the
driver stubbed, so no test scans the developer's real disk."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import worktree_gc as gc
from backend.web.core import worktree_gc_driver as drv

DAY = 24 * 3600.0

client = TestClient(server.app)
local_client = TestClient(
    server.app, client=("127.0.0.1", 50000), headers={"host": "127.0.0.1"}
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("MINDFLOCK_HOME", str(h))
    monkeypatch.setattr(drv, "_disk_instances", lambda: [])
    monkeypatch.setattr(drv, "_sleeping", lambda: set())
    monkeypatch.setattr(drv, "_clone_roots", lambda: [])
    drv._report.clear()
    # A removal closes editor windows on the folder (xdotool, by title):
    # never let a test reach the real desktop.
    from backend.web.core import cursor_windows

    monkeypatch.setattr(cursor_windows, "_close_cursor_window", lambda path: None)
    return h


class _WT:
    def __init__(self, repo, branch, existing=False):
        self.repo, self.branch, self.existing = repo, branch, existing

    def GetRepoPath(self):
        return self.repo

    def GetBranchName(self):
        return self.branch

    def IsExistingBranch(self):
        return self.existing


class _Inst:
    def __init__(self, title, path, status=0, repo="/r", branch="b"):
        self.Title, self._path, self.Status = title, path, status
        self.UpdatedAt = dt.datetime.now(dt.timezone.utc)
        self.base_branch = "main"
        self._wt = _WT(repo, branch)

    def GetWorktreePath(self):
        return self._path

    def GetGitWorktree(self):
        return self._wt


def _srv(instances=None, closed=None):
    return SimpleNamespace(
        ENGINE=SimpleNamespace(instances=instances or {}),
        _load_recently_closed=lambda: list(closed or []),
        log=SimpleNamespace(InfoLog=None, ErrorLog=None),
    )


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


# ------------------------------------------------------------- who uses what


def test_live_paused_and_sleeping_sessions_are_in_use(home, tmp_path, monkeypatch):
    a, b, c = (str(tmp_path / n) for n in ("a", "b", "c"))
    monkeypatch.setattr(drv, "_sleeping", lambda: {"zz"})
    srv = _srv(
        {"run": _Inst("run", a), "pz": _Inst("pz", b, status=3), "zz": _Inst("zz", c)}
    )
    u = drv.build_usage(srv)
    assert u.in_use[gc.norm(a)] == "session 'run' (running)"
    assert u.in_use[gc.norm(b)] == "session 'pz' (paused)"
    assert u.in_use[gc.norm(c)] == "session 'zz' (sleeping)"
    assert gc.norm("/r") in u.repos
    assert u.branch_info[gc.norm(a)]["branch"] == "b"


def test_another_servers_sessions_in_state_json_are_in_use(home, tmp_path, monkeypatch):
    p = str(tmp_path / "other")
    monkeypatch.setattr(
        drv,
        "_disk_instances",
        lambda: [
            {
                "title": "theirs",
                "status": 1,
                "worktree": {
                    "worktree_path": p,
                    "repo_path": "/r2",
                    "branch_name": "x",
                },
            }
        ],
    )
    u = drv.build_usage(None)  # the CLI with no server: files only
    assert u.in_use[gc.norm(p)] == "session 'theirs' (ready)"


def test_a_recently_closed_session_keeps_its_folder_an_old_one_does_not(home, tmp_path):
    now = time.time()
    recent, old = str(tmp_path / "recent"), str(tmp_path / "old")
    closed = [
        {
            "title": "r",
            "folder": recent,
            "closed_at": _iso(now - 2 * DAY),
            "data": {"worktree": {"repo_path": "/r"}},
        },
        {"title": "o", "folder": old, "closed_at": _iso(now - 40 * DAY), "data": {}},
    ]
    u = drv.build_usage(_srv(closed=closed), after_days=14, now=now)
    assert "closed 2 days ago" in u.in_use[gc.norm(recent)]
    assert gc.norm(old) not in u.in_use
    assert u.last_used[gc.norm(old)] == pytest.approx(now - 40 * DAY, abs=1)


def test_an_unfinished_route_or_run_keeps_its_session_folder(
    home, tmp_path, monkeypatch
):
    from backend.web.core import flights, team_runs

    p, q = str(tmp_path / "flown"), str(tmp_path / "run")
    closed = [
        {"title": "flown", "folder": p, "closed_at": _iso(time.time() - 90 * DAY)},
        {"title": "worker", "folder": q, "closed_at": _iso(time.time() - 90 * DAY)},
    ]
    monkeypatch.setattr(
        flights,
        "list_flights",
        lambda include_finished=True: [
            {"id": "f1", "plan_name": "Ship it", "state": "paused", "session": "flown"}
        ],
    )
    monkeypatch.setattr(
        team_runs,
        "list_runs",
        lambda include_finished=True: [
            {
                "id": "r1",
                "name": "Run",
                "tasks": [{"title": "worker", "state": "working"}],
            }
        ],
    )
    u = drv.build_usage(_srv(closed=closed))
    assert u.in_use[gc.norm(p)] == "route 'Ship it' (paused)"
    assert u.in_use[gc.norm(q)] == "team run 'Run' (working)"


def test_the_pr_rung_needs_merged_at_this_very_head(home):
    srv = _srv()
    srv._pr_info = lambda wt, branch: {"state": "MERGED", "head": "abc"}
    u = drv.build_usage(srv)
    assert u.pr_merged({"path": "/x", "branch": "b", "head": "abc"})
    assert not u.pr_merged({"path": "/x", "branch": "b", "head": "def"})


# ----------------------------------------------------------------- settings


def test_the_setting_defaults_to_ask_after_14_days_and_round_trips(home):
    assert drv.settings() == {"mode": "ask", "after_days": 14}
    assert drv.set_settings("auto", 30) == {"mode": "auto", "after_days": 30}
    assert drv.settings() == {"mode": "auto", "after_days": 30}
    with pytest.raises(ValueError):
        drv.set_settings("sometimes")
    with pytest.raises(ValueError):
        drv.set_settings(None, 0)


# ------------------------------------------------------------ notice + pass


def _rep(count, nbytes):
    return {"summary": {"safe_count": count, "safe_bytes": nbytes}}


def test_ask_mode_tells_once_and_again_only_after_a_collection(home, monkeypatch):
    from backend.web.core import events

    seen = []
    monkeypatch.setattr(events.BUS, "emit", lambda name, **kw: seen.append((name, kw)))
    assert not drv.maybe_notify(None, _rep(2, 10 * 1024**2))  # not worth it
    assert drv.maybe_notify(None, _rep(12, 10 * 1024**2))
    assert not drv.maybe_notify(None, _rep(30, 50 * 1024**3))  # said already
    assert len(seen) == 1
    name, kw = seen[0]
    assert name == "system.cleanup_available" and kw["data"]["count"] == 12
    assert "Settings → Disk cleanup" in kw["data"]["detail"]
    st = drv._state()
    st["notified"] = False  # what a collection does
    drv._save_state(st)
    assert drv.maybe_notify(None, _rep(1, 3 * 1024**3))
    assert len(seen) == 2


def test_the_pass_follows_the_mode(home, monkeypatch):
    calls = []
    monkeypatch.setattr(
        drv,
        "collect_now",
        lambda srv, **kw: calls.append(("collect", kw)) or {"removed": []},
    )
    monkeypatch.setattr(
        drv, "scan", lambda srv, **kw: calls.append(("scan",)) or _rep(0, 0)
    )
    drv.set_settings("off")
    assert drv.pass_once(_srv()) == {"mode": "off"} and calls == []
    drv.set_settings("ask")
    assert drv.pass_once(_srv())["mode"] == "ask" and calls == [("scan",)]
    drv.set_settings("auto")
    calls.clear()
    assert drv.pass_once(_srv())["mode"] == "auto"
    assert calls == [("collect", {"limit": gc.MAX_PER_PASS, "log": None})]


def test_the_loop_is_off_under_pytest_and_by_env(monkeypatch):
    monkeypatch.setattr(drv, "ENABLED", None)
    assert drv.enabled() is False
    monkeypatch.setattr(drv, "ENABLED", True)
    assert drv.enabled() is True


# ------------------------------------------------- an end-to-end pass, temp


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


def test_collect_now_removes_only_the_safe_and_forgets_closed_rows(
    home, tmp_path, monkeypatch
):
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text("[user]\n\tname = t\n\temail = t@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gcfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("1")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "c")
    root = home / "worktrees" / "u"
    safe, busy = str(root / "safe"), str(root / "busy")
    _git(repo, "worktree", "add", "-q", "-b", "u/safe", safe)
    _git(repo, "worktree", "add", "-q", "-b", "u/busy", busy)
    old = time.time() - 60 * DAY
    for p in (safe, busy):
        os.utime(p, (old, old))
        gd = gc.gitdir_of(p)
        for n in ("HEAD", "index", os.path.join("logs", "HEAD")):
            if os.path.exists(os.path.join(gd, n)):
                os.utime(os.path.join(gd, n), (old, old))
    closed = [
        {
            "title": "safe",
            "folder": safe,
            "closed_at": _iso(old),
            "data": {"worktree": {"repo_path": str(repo)}},
        }
    ]
    saved = []
    srv = _srv({"busy": _Inst("busy", busy, repo=str(repo), branch="u/busy")}, closed)
    srv._save_recently_closed = lambda entries: saved.append(entries)
    res = drv.collect_now(srv)
    assert [r["path"] for r in res["removed"]] == [gc.norm(safe)]
    assert not os.path.exists(safe) and os.path.isdir(busy)
    assert saved == [[]]  # the closed row that could only 410 is gone
    assert not (home / "worktrees" / "u" / "safe").exists()
    assert drv._state()["last_collect"]["removed"] == 1


# ------------------------------------------------------------------- routes


def test_get_cleanup_returns_the_report(monkeypatch):
    seen = []
    monkeypatch.setattr(
        drv,
        "report",
        lambda srv, refresh=False: seen.append(refresh)
        or {"items": [], "scanning": False},
    )
    r = client.get("/api/cleanup?refresh=1")
    assert r.status_code == 200 and r.json()["items"] == [] and seen == [True]


def test_removing_and_configuring_is_owner_only(monkeypatch):
    monkeypatch.setattr(
        drv,
        "collect_now",
        lambda srv: {
            "removed": [],
            "skipped": [],
            "branches_deleted": [],
            "freed_bytes": 0,
        },
    )
    for path, body in (
        ("/api/cleanup/collect", {}),
        ("/api/cleanup/remove", {"id": "x"}),
        ("/api/cleanup/settings", {"mode": "auto"}),
    ):
        assert client.post(path, json=body).status_code == 403, path
    r = local_client.post("/api/cleanup/collect", json={})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_remove_route_maps_outcomes_to_status_codes(monkeypatch):
    out = {}
    monkeypatch.setattr(
        drv,
        "remove_item",
        lambda srv, item_id, confirm: dict(out[item_id], confirm=confirm),
    )
    out.update(
        gone={"ok": False, "missing": True},
        work={"ok": False, "needs_confirm": True, "error": "holds work"},
        done={"ok": True},
    )
    assert local_client.post("/api/cleanup/remove", json={}).status_code == 400
    assert (
        local_client.post("/api/cleanup/remove", json={"id": "gone"}).status_code == 404
    )
    assert (
        local_client.post("/api/cleanup/remove", json={"id": "work"}).status_code == 409
    )
    r = local_client.post("/api/cleanup/remove", json={"id": "done", "confirm": True})
    assert r.status_code == 200 and r.json()["confirm"] is True


def test_settings_route_validates(home):
    r = local_client.post("/api/cleanup/settings", json={"mode": "sometimes"})
    assert r.status_code == 400
    r = local_client.post(
        "/api/cleanup/settings", json={"mode": "off", "after_days": 21}
    )
    assert r.status_code == 200 and r.json() == {"mode": "off", "after_days": 21}
