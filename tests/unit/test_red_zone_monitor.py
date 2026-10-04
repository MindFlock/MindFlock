"""Red-zone reconcile loop (``backend.web.core.red_zone_monitor``).

The event contract under test is the part the design critique insisted on:
first sight is SEEDED (a restart never re-announces), a block is announced
once per (session, zone) per work cycle, a breach once per (worktree, path)
however many sessions share the worktree, and "tampered" fires only on a
transition away from armed.

Every test runs against a REAL throwaway git repo (the breach set is git's
change set), with the zone store / guard dir / feed dir / activity markers
redirected to tmp. The monitor is driven by calling ``tick`` directly with
fake instances — never through the lifespan loop, never over ``ENGINE``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from backend.config import red_zones
from backend.providers import activity_markers as am
from backend.web import server
from backend.web.core import code_map
from backend.web.core import events as _events
from backend.web.core import red_zone_monitor as mon


def _git(cwd, *args) -> str:
    cp = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout
    return cp.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class _Inst:
    """The slice of an Instance the monitor reads."""

    def __init__(self, title: str, wt: Path, program: str = "claude"):
        self.Title = title
        self.Program = program
        self.Status = None
        self.Path = str(wt)
        self.Branch = "feature"
        self._wt = str(wt)

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setattr(mon, "BREACH_MIN_INTERVAL_S", 0.0)
    mon.reset_for_tests()
    for name in ("_LIST_CACHE", "_CHANGED_CACHE", "_PLAN_LATCH", "_TP_LATCH"):
        getattr(code_map, name).clear()
    yield
    mon.reset_for_tests()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo with an initial commit; the fork point is pinned to it."""
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _write(r, "app.py", "print(1)\n")
    _write(r, "config/settings.toml", "a = 1\n")
    _write(r, ".gitignore", "local.env\n")
    _write(r, "local.env", "SECRET=1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    base = _git(r, "rev-parse", "HEAD")
    monkeypatch.setattr(server, "_session_fork_point", lambda inst, wt: base)
    return r


@pytest.fixture
def got():
    seen: list = []

    def _cb(env):
        if str(env.get("event", "")).startswith("session.red_zone"):
            seen.append(env)

    unsub = _events.BUS.subscribe(_cb)
    yield seen
    unsub()


def _repo_id(r: Path) -> str:
    return red_zones.repo_identity(str(r))[0]


def _feed(title: str, *records: dict) -> None:
    from backend.session import tmux

    p = red_zones.feed_path(tmux.to_mindflock_tmux_name(title))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _deny(ts: float, zone_id: str, pattern: str, path: str) -> dict:
    return {
        "v": 1,
        "ts": ts,
        "ev": "pre",
        "tool": "Edit",
        "kind": "edit",
        "id": "t%s" % ts,
        "deny": {
            "path": path,
            "pattern": pattern,
            "name": "",
            "zone_id": zone_id,
            "reason": "no",
        },
    }


def _events_of(got, name):
    return [e for e in got if e["event"] == name]


# --------------------------------------------------------------------------- #
# Blocked: seeded, then once per (session, zone) per work cycle
# --------------------------------------------------------------------------- #
def test_feed_is_seeded_silently_then_new_denies_emit(repo, got):
    inst = _Inst("rz-a", repo, program="bash")
    old = time.time() - 60
    _feed("rz-a", _deny(old, "rz_1", "config/", "config/settings.toml"))
    mon.tick({"rz-a": inst}, {"rz-a": "working"})
    assert _events_of(got, "session.red_zone_blocked") == []  # history, not news

    now = time.time()
    _feed(
        "rz-a",
        _deny(now, "rz_1", "config/", "config/settings.toml"),
        _deny(now + 0.01, "rz_1", "config/", "config/other.toml"),
    )
    mon.tick({"rz-a": inst}, {"rz-a": "working"})
    (ev,) = _events_of(got, "session.red_zone_blocked")
    assert ev["session"] == "rz-a"
    d = ev["data"]
    assert d["count"] == 2
    assert d["zone_ids"] == ["rz_1"]
    assert d["patterns"] == ["config/"]
    assert d["paths"] == ["config/settings.toml", "config/other.toml"]
    assert d["tool"] == "Edit"
    assert d["detail"] == "blocked 2 edits to config/"


def test_blocked_is_once_per_zone_per_work_cycle(repo, got):
    inst = _Inst("rz-b", repo, program="bash")
    mon.tick({"rz-b": inst}, {"rz-b": "working"})  # seed (empty feed)
    t = time.time()
    _feed("rz-b", _deny(t, "rz_1", "config/", "config/settings.toml"))
    mon.tick({"rz-b": inst}, {"rz-b": "working"})
    _feed("rz-b", _deny(t + 1, "rz_1", "config/", "config/settings.toml"))
    mon.tick({"rz-b": inst}, {"rz-b": "working"})
    assert len(_events_of(got, "session.red_zone_blocked")) == 1
    # A DIFFERENT zone in the same cycle is its own news.
    _feed("rz-b", _deny(t + 2, "rz_2", "app.py", "app.py"))
    mon.tick({"rz-b": inst}, {"rz-b": "working"})
    blocked = _events_of(got, "session.red_zone_blocked")
    assert [e["data"]["zone_ids"] for e in blocked] == [["rz_1"], ["rz_2"]]
    # The agent went idle: the cycle closes; the next turn's attempt re-announces.
    mon.tick({"rz-b": inst}, {"rz-b": "idle"})
    _feed("rz-b", _deny(t + 3, "rz_1", "config/", "config/settings.toml"))
    mon.tick({"rz-b": inst}, {"rz-b": "working"})
    assert len(_events_of(got, "session.red_zone_blocked")) == 3
    s = mon.summary("rz-b")
    assert s is not None and s["last_block_ts"] == pytest.approx(t + 3)


# --------------------------------------------------------------------------- #
# Breached: per WORKTREE, seeded, once per path
# --------------------------------------------------------------------------- #
def test_breach_is_seeded_then_emitted_once_per_worktree_path(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    # Already changed before the loop ever looked: seeded, never announced.
    _write(repo, "config/settings.toml", "a = 2\n")
    a, b = _Inst("rz-c", repo, "bash"), _Inst("rz-c-copy", repo, "bash")
    mon.tick({"rz-c": a, "rz-c-copy": b}, {})
    assert _events_of(got, "session.red_zone_breached") == []
    assert mon.summary("rz-c")["breaches"] == 1

    _write(repo, "config/new.toml", "x = 1\n")  # a fresh breach
    _write(repo, "app.py", "print(2)\n")  # outside every zone
    mon.tick({"rz-c": a, "rz-c-copy": b}, {})
    (ev,) = _events_of(got, "session.red_zone_breached")
    # Two windows on one worktree: ONE event, on the first title.
    assert ev["session"] == "rz-c"
    assert ev["data"]["paths"] == ["config/new.toml"]
    assert ev["data"]["patterns"] == ["config/"]
    assert ev["data"]["total"] == 2
    assert "config/new.toml" in ev["data"]["detail"]
    # Still breached next tick: not re-announced.
    _write(repo, "config/new.toml", "x = 2\n")
    mon.tick({"rz-c": a, "rz-c-copy": b}, {})
    assert len(_events_of(got, "session.red_zone_breached")) == 1
    assert mon.summary("rz-c-copy")["breaches"] == 2


def test_a_zone_created_over_changed_files_is_silent(repo, got):
    inst = _Inst("rz-d", repo, "bash")
    _write(repo, "app.py", "print(2)\n")
    mon.tick({"rz-d": inst}, {})
    red_zones.add_zone("repo", _repo_id(repo), "app.py")
    mon.tick({"rz-d": inst}, {})
    assert _events_of(got, "session.red_zone_breached") == []
    assert mon.summary("rz-d")["breaches"] == 1


def test_route_seeded_breaches_are_not_announced(repo, got):
    inst = _Inst("rz-e", repo, "bash")
    mon.tick({"rz-e": inst}, {})
    _write(repo, "app.py", "print(3)\n")
    mon.seed_breaches(str(repo), ["app.py"])
    red_zones.add_zone("worktree", str(repo), "app.py", repo_id=_repo_id(repo))
    mon.tick({"rz-e": inst}, {})
    assert _events_of(got, "session.red_zone_breached") == []


def test_ignored_zoned_file_breach_tracks_content_not_mtime(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "local.env")
    inst = _Inst("rz-f", repo, "bash")
    mon.tick({"rz-f": inst}, {})  # baseline of the git-ignored file
    assert (mon.summary("rz-f") or {}).get("breaches", 0) == 0
    _write(repo, "local.env", "SECRET=2\n")
    mon.tick({"rz-f": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_breached")
    assert ev["data"]["paths"] == ["local.env"]
    assert set(mon.ignored_breaches(str(repo))) == {"local.env"}
    # Reverted, as the block reason tells the agent to: no longer a breach.
    _write(repo, "local.env", "SECRET=1\n")
    mon.tick({"rz-f": inst}, {})
    assert mon.ignored_breaches(str(repo)) == {}
    assert mon.summary("rz-f")["breaches"] == 0


def test_bash_backstop_breach_records_are_announced(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "local.env")
    inst = _Inst("rz-g", repo, "bash")
    mon.tick({"rz-g": inst}, {})
    _write(repo, "local.env", "SECRET=from-a-shell-write\n")
    _feed(
        "rz-g",
        {
            "v": 1,
            "ts": time.time(),
            "ev": "post",
            "tool": "Bash",
            "kind": "bash",
            "id": "b1",
            "breach": [{"path": "local.env", "pattern": "local.env"}],
        },
    )
    mon.tick({"rz-g": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_breached")
    assert ev["data"]["paths"] == ["local.env"]


def test_committed_breaches_reach_the_guard_file(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-h", repo, "bash")
    mon.tick({"rz-h": inst}, {})
    _write(repo, "config/settings.toml", "a = 9\n")
    _git(repo, "commit", "-qam", "touch config")
    mon.tick({"rz-h": inst}, {})
    guard = json.loads(Path(red_zones.guard_path(str(repo))).read_text())
    assert guard["breaches"] == ["config/settings.toml"]
    assert [r["pattern"] for r in guard["rules"]] == ["config/"]
    assert mon.committed_for(str(repo)) == ["config/settings.toml"]
    # A route-triggered resync keeps the committed breaches (never opens the gate).
    mon.resync([(str(repo), str(repo))])
    guard = json.loads(Path(red_zones.guard_path(str(repo))).read_text())
    assert guard["breaches"] == ["config/settings.toml"]


# --------------------------------------------------------------------------- #
# Guard + hooks tamper detection
# --------------------------------------------------------------------------- #
def test_guard_rewritten_outside_mindflock_is_restored_and_reported(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-i", repo, "bash")
    mon.tick({"rz-i": inst}, {})
    gp = Path(red_zones.guard_path(str(repo)))
    doc = json.loads(gp.read_text())
    assert doc["rules"]
    # The agent (somehow) empties the rules.
    gp.write_text(json.dumps(dict(doc, rules=[])))
    mon.tick({"rz-i": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_tampered")
    assert ev["session"] == "rz-i" and ev["data"]["what"] == "guard"
    assert json.loads(gp.read_text())["rules"]  # healed


def test_guard_deleted_is_reported(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-j", repo, "bash")
    mon.tick({"rz-j": inst}, {})
    gp = Path(red_zones.guard_path(str(repo)))
    gp.unlink()
    mon.tick({"rz-j": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_tampered")
    assert ev["data"]["what"] == "guard"
    assert gp.exists()


def test_benign_cross_process_sync_is_not_tampering(repo, got):
    """Another MindFlock process syncing the same root (a CLI resume) writes
    the same rules with different breaches — that is not an attack."""
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-k", repo, "bash")
    mon.tick({"rz-k": inst}, {})
    gp = Path(red_zones.guard_path(str(repo)))
    doc = json.loads(gp.read_text())
    gp.write_text(json.dumps(dict(doc, breaches=["config/x"], lroot="/else")))
    mon.tick({"rz-k": inst}, {})
    assert _events_of(got, "session.red_zone_tampered") == []


def _settings(repo: Path) -> Path:
    return repo / ".claude" / "settings.local.json"


def test_hooks_first_arm_is_silent_and_a_disarm_is_healed_and_reported(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-l", repo, "claude")
    assert not _settings(repo).exists()
    mon.tick({"rz-l": inst}, {})
    assert am.hooks_armed(_settings(repo))
    assert _events_of(got, "session.red_zone_tampered") == []  # first arm
    # The agent strips the tool hook out of its own settings file.
    _settings(repo).write_text("{}")
    mon.tick({"rz-l": inst}, {})
    assert am.hooks_armed(_settings(repo))  # re-pinned
    (ev,) = _events_of(got, "session.red_zone_tampered")
    assert ev["data"]["what"] == "hooks"


def test_project_disable_all_hooks_is_healed_and_reported(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-m", repo, "claude")
    mon.tick({"rz-m": inst}, {})
    # Strip the local override, then disable hooks project-wide.
    data = json.loads(_settings(repo).read_text())
    data.pop("disableAllHooks", None)
    _settings(repo).write_text(json.dumps(data))
    (repo / ".claude" / "settings.json").write_text('{"disableAllHooks": true}')
    assert not am.hooks_armed(_settings(repo))
    mon.tick({"rz-m": inst}, {})
    assert am.hooks_armed(_settings(repo))  # local false resists project true
    assert [e["data"]["what"] for e in got] == ["hooks"]


def test_a_pre_feature_session_is_armed_even_without_zones(repo, got):
    inst = _Inst("rz-n", repo, "claude")
    mon.tick({"rz-n": inst}, {})
    assert am.hooks_armed(_settings(repo))
    assert got == []
    assert mon.summary("rz-n") is None  # no zones, nothing to say


def test_detect_only_provider_hooks_are_left_alone(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-o", repo, "codex")
    mon.tick({"rz-o": inst}, {})
    assert not _settings(repo).exists()
    assert mon.summary("rz-o")["guard"] == "detect"


# --------------------------------------------------------------------------- #
# Row summary guard states
# --------------------------------------------------------------------------- #
def test_summary_none_without_zones(repo):
    inst = _Inst("rz-p", repo, "bash")
    mon.tick({"rz-p": inst}, {})
    assert mon.summary("rz-p") is None
    assert mon.summary("never-seen") is None


def test_summary_guarded_then_arming_then_guarded(repo):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-q", repo, "claude")
    mon.tick({"rz-q": inst}, {})
    s = mon.summary("rz-q")
    assert s == {
        "zones": 1,
        "breaches": 0,
        "last_block_ts": None,
        "guard": "guarded",
        "mode": "red",
    }
    # A hook fired well after arming (working marker) but no feed record yet.
    armed_at = os.stat(_settings(repo)).st_mtime
    from backend.session import tmux

    tm = tmux.to_mindflock_tmux_name("rz-q")
    mp = am.marker_path(tm)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"state": "working", "ts": int(armed_at) + 30}))
    mon.tick({"rz-q": inst}, {})
    assert mon.summary("rz-q")["guard"] == "arming"
    # The first feed record since arming proves the tool hook is live.
    _feed(
        "rz-q",
        {"v": 1, "ts": time.time(), "ev": "pre", "tool": "Read", "kind": "read"},
    )
    mon.tick({"rz-q": inst}, {})
    assert mon.summary("rz-q")["guard"] == "guarded"


def test_summary_off_when_the_hooks_cannot_be_healed(repo, monkeypatch):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    from backend import providers

    prov = providers.resolve("claude")
    monkeypatch.setattr(type(prov), "install_activity_hooks", lambda self, w, s: None)
    inst = _Inst("rz-r", repo, "claude")
    mon.tick({"rz-r": inst}, {})
    assert mon.summary("rz-r")["guard"] == "off"
    info = mon.guard_info("rz-r", inst, 1)
    assert info == {
        "state": "off",
        "detail": "The red-zone hook was removed or disabled — MindFlock is "
        "re-arming the guard",
        "hard": True,
        "mode": "red",
    }


def test_guard_info_before_the_first_tick(repo):
    inst = _Inst("rz-s", repo, "codex")
    assert mon.guard_info("rz-s", inst, 0)["state"] == "none"
    info = mon.guard_info("rz-s", inst, 2)
    assert info["state"] == "detect" and info["hard"] is False
    assert info["detail"] == (
        "Codex can't be stopped before an edit — red-zone changes are "
        "detected, flagged and block pushes"
    )


# --------------------------------------------------------------------------- #
# Store tamper, forget, robustness
# --------------------------------------------------------------------------- #
def test_store_changed_outside_a_route_is_reported_once(repo, got):
    inst = _Inst("rz-t", repo, "bash")
    rid = _repo_id(repo)
    mon.tick({"rz-t": inst}, {})  # housekeeping baseline
    # Through a route: ours, silent.
    with mon.route_write():
        red_zones.add_zone("repo", rid, "config/")
    mon._HK["at"] = 0.0
    mon._STORE["route_at"] = 0.0
    mon.tick({"rz-t": inst}, {})
    assert _events_of(got, "session.red_zone_tampered") == []
    # Behind our back.
    red_zones.add_zone("repo", rid, "app.py")
    mon._HK["at"] = 0.0
    mon.tick({"rz-t": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_tampered")
    assert ev["session"] == "rz-t" and ev["data"]["what"] == "store"
    mon._HK["at"] = 0.0
    mon.tick({"rz-t": inst}, {})
    assert len(_events_of(got, "session.red_zone_tampered")) == 1


def test_forget_drops_worktree_zones_guard_and_feed(repo):
    from backend.session import tmux

    red_zones.add_zone("worktree", str(repo), "app.py", repo_id=_repo_id(repo))
    inst = _Inst("rz-u", repo, "bash")
    mon.tick({"rz-u": inst}, {})
    _feed("rz-u", {"v": 1, "ts": time.time(), "ev": "pre", "tool": "Read"})
    gp = Path(red_zones.guard_path(str(repo)))
    assert gp.exists()
    # Shared worktree: the zone store + guard stay; the title's own state goes.
    mon.forget("rz-u", str(repo), in_use_by_other=True)
    assert gp.exists() and red_zones.worktree_zones(str(repo))
    assert not os.path.exists(red_zones.feed_path(tmux.to_mindflock_tmux_name("rz-u")))
    mon.forget("rz-u", str(repo), in_use_by_other=False)
    assert not gp.exists()
    assert red_zones.worktree_zones(str(repo)) == []


def test_tick_never_raises_and_skips_unusable_instances(repo, got):
    class _Broken:
        Program = "bash"

        def Started(self):  # noqa: N802
            raise RuntimeError("boom")

    class _NoDir(_Inst):
        def GetWorktreePath(self):  # noqa: N802
            return "/no/such/dir"

    mon.tick({"x": _Broken(), "y": _NoDir("y", repo)}, None)
    mon.tick(None)
    assert got == []


def test_paused_titles_keep_their_worktree_baseline(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-v", repo, "bash")
    mon.tick({"rz-v": inst}, {})
    from backend.session.storage import Paused

    inst.Status = Paused
    mon.tick({"rz-v": inst}, {})
    assert os.path.realpath(str(repo)) in mon._ROOTS
    mon.tick({}, {})  # deleted without the route: pruned
    assert os.path.realpath(str(repo)) not in mon._ROOTS
    assert mon.summary("rz-v") is None


# --------------------------------------------------------------------------- #
# Breach verification: first-sight baselines, backstop records as evidence
# --------------------------------------------------------------------------- #
def _backstop(title: str, *paths: str, pattern: str = "") -> None:
    _feed(
        title,
        {
            "v": 1,
            "ts": time.time(),
            "ev": "post",
            "tool": "Bash",
            "kind": "bash",
            "id": "b%s" % time.time(),
            "breach": [{"path": p, "pattern": pattern or p} for p in paths],
        },
    )


def _pkg_repo(repo: Path, monkeypatch) -> None:
    """A zoned Python package whose caches are git-ignored — committed as
    part of the fork point, so it is the baseline, not work on the branch."""
    _write(repo, "athena/__init__.py", "")
    _write(repo, "athena/core.py", "X = 1\n")
    _write(repo, ".gitignore", "local.env\n__pycache__/\n*.pyc\nconfig/*.local\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "athena")
    base = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(server, "_session_fork_point", lambda inst, wt: base)


def test_build_artifacts_that_appear_in_a_zone_are_not_breaches(repo, got, monkeypatch):
    """F19: a fresh worktree has no __pycache__; running the tests creates
    it. A newly LISTED ignored file is baselined as it is at first sight — it
    was never the agent's edit — so the re-list is silent."""
    _pkg_repo(repo, monkeypatch)
    red_zones.add_zone("repo", _repo_id(repo), "athena")
    inst = _Inst("rz-w", repo, "bash")
    mon.tick({"rz-w": inst}, {})  # seed
    _write(repo, "athena/__pycache__/core.cpython-312.pyc", "\x00bytecode")
    monkeypatch.setattr(mon, "IGNORED_REFRESH_S", 0.0)
    mon.tick({"rz-w": inst}, {})
    mon.tick({"rz-w": inst}, {})
    assert _events_of(got, "session.red_zone_breached") == []
    assert mon.summary("rz-w")["breaches"] == 0
    # …while a CHANGE to that file after its first sight still counts.
    _write(repo, "athena/__pycache__/core.cpython-312.pyc", "\x00tampered")
    mon.tick({"rz-w": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_breached")
    assert ev["data"]["paths"] == ["athena/__pycache__/core.cpython-312.pyc"]


@pytest.mark.parametrize("how", ["touch", "unchanged"])
def test_backstop_record_for_a_file_git_says_is_unchanged_is_dropped(
    repo, got, how, monkeypatch
):
    """F20: the backstop said "athena/core.py" (its dir mtime moved, or the
    command touched it) but git's view is that nothing changed there."""
    _pkg_repo(repo, monkeypatch)
    red_zones.add_zone("repo", _repo_id(repo), "athena")
    inst = _Inst("rz-x", repo, "bash")
    mon.tick({"rz-x": inst}, {})
    if how == "touch":
        os.utime(repo / "athena" / "core.py", (time.time() + 5, time.time() + 5))
    _backstop("rz-x", "athena/core.py", "athena/__init__.py", pattern="athena")
    mon.tick({"rz-x": inst}, {})
    assert _events_of(got, "session.red_zone_breached") == []
    assert mon.summary("rz-x")["breaches"] == 0


def test_backstop_record_for_a_touched_ignored_file_is_dropped(repo, got):
    """…nor when the monitor's content baseline says it is unchanged."""
    red_zones.add_zone("repo", _repo_id(repo), "local.env")
    inst = _Inst("rz-y", repo, "bash")
    mon.tick({"rz-y": inst}, {})
    os.utime(repo / "local.env", (time.time() + 5, time.time() + 5))
    _backstop("rz-y", "local.env")
    mon.tick({"rz-y": inst}, {})
    assert _events_of(got, "session.red_zone_breached") == []


def test_backstop_record_for_a_created_then_deleted_path_is_dropped(repo, got):
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-z", repo, "bash")
    mon.tick({"rz-z": inst}, {})
    _backstop("rz-z", "config/tmp.toml", pattern="config/")  # already gone
    mon.tick({"rz-z": inst}, {})
    assert _events_of(got, "session.red_zone_breached") == []


def test_backstop_evidence_flags_an_ignored_file_an_agent_created(
    repo, got, monkeypatch
):
    """The one case only the backstop can prove: a git-ignored zoned file
    that did not exist at the last complete listing and that the hook saw an
    agent command create. Announced, and honest about what it blocks."""
    _pkg_repo(repo, monkeypatch)
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-aa", repo, "bash")
    mon.tick({"rz-aa": inst}, {})
    _write(repo, "config/secrets.local", "TOKEN=x\n")
    _backstop("rz-aa", "config/secrets.local", pattern="config/")
    mon.tick({"rz-aa": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_breached")
    assert ev["data"]["paths"] == ["config/secrets.local"]
    assert ev["data"]["blocks_push"] is False
    assert "git-ignored, so never pushed" in ev["data"]["detail"]
    assert set(mon.ignored_breaches(str(repo))) == {"config/secrets.local"}
    # Deleted again: no longer a breach.
    (repo / "config" / "secrets.local").unlink()
    mon.tick({"rz-aa": inst}, {})
    assert mon.ignored_breaches(str(repo)) == {}


def test_backstop_path_in_a_nested_sandbox_is_verified_by_its_own_git(repo, got):
    """Claude's EnterWorktree checkout (.claude/worktrees/<n>) is invisible to
    the session worktree's git; that checkout's own status vouches for it."""
    (repo / ".claude").mkdir()
    _git(repo, "worktree", "add", "-q", "-b", "sbx", ".claude/worktrees/w1")
    nested = repo / ".claude" / "worktrees" / "w1"
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-ab", repo, "bash")
    mon.tick({"rz-ab": inst}, {})
    # Unchanged in the sandbox: dropped.
    _backstop("rz-ab", ".claude/worktrees/w1/config/settings.toml", pattern="config/")
    mon.tick({"rz-ab": inst}, {})
    assert _events_of(got, "session.red_zone_breached") == []
    # Really changed there: announced, under the zone it matches.
    _write(nested, "config/settings.toml", "a = 42\n")
    _backstop("rz-ab", ".claude/worktrees/w1/config/settings.toml", pattern="config/")
    mon.tick({"rz-ab": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_breached")
    assert ev["data"]["paths"] == [".claude/worktrees/w1/config/settings.toml"]
    assert ev["data"]["patterns"] == ["config/"]


def test_a_write_landing_during_the_diff_is_not_hidden_by_the_fingerprint(
    repo, got, monkeypatch
):
    """The fingerprint that gates the next recompute is the one taken BEFORE
    the diff: a write that lands while changed_files runs must move it."""
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-ac", repo, "bash")
    mon.tick({"rz-ac": inst}, {})
    real = code_map.changed_files
    fired = []

    def _racing(i, w):
        out = real(i, w)
        if not fired:
            fired.append(1)
            _write(repo, "config/late.toml", "x = 1\n")  # lands mid-pass
        return out

    monkeypatch.setattr(code_map, "changed_files", _racing)
    _write(repo, "config/settings.toml", "a = 2\n")
    mon.tick({"rz-ac": inst}, {})
    monkeypatch.setattr(code_map, "changed_files", real)
    mon.tick({"rz-ac": inst}, {})
    paths = [
        p
        for e in _events_of(got, "session.red_zone_breached")
        for p in e["data"]["paths"]
    ]
    assert "config/late.toml" in paths


# --------------------------------------------------------------------------- #
# Reseed scope (a zone-set change)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("change", ["add", "waive"])
def test_an_unrelated_zone_change_keeps_an_ignored_breach(repo, got, change):
    """F22a: the reseed used to rebuild every ignored baseline from the
    CURRENT (edited) content, erasing the breach for good."""
    rid = _repo_id(repo)
    red_zones.add_zone("repo", rid, "local.env")
    red_zones.add_zone("repo", rid, "app.py")
    inst = _Inst("rz-ad", repo, "bash")
    mon.tick({"rz-ad": inst}, {})
    _write(repo, "local.env", "SECRET=2 agent\n")
    mon.tick({"rz-ad": inst}, {})
    assert set(mon.ignored_breaches(str(repo))) == {"local.env"}
    if change == "add":
        red_zones.add_zone("repo", rid, "config/")
        mon.seed_breaches(str(repo), [])  # what the add route does
    else:
        zid = next(
            z["id"] for z in red_zones.repo_zones(rid) if z["pattern"] == "app.py"
        )
        red_zones.set_waiver(str(repo), zid, True)
    for _ in range(3):
        mon.tick({"rz-ad": inst}, {})
    assert set(mon.ignored_breaches(str(repo))) == {"local.env"}
    assert mon.summary("rz-ad")["breaches"] == 1


def test_a_pending_breach_of_an_existing_zone_survives_a_zone_add(
    repo, got, monkeypatch
):
    """F22b: throttled, the edit hasn't been diffed yet when the user adds
    an unrelated zone; the reseed must absorb only what the NEW zone covers."""
    monkeypatch.setattr(mon, "BREACH_MIN_INTERVAL_S", 10.0)
    rid = _repo_id(repo)
    red_zones.add_zone("repo", rid, "app.py")
    _write(repo, "config/settings.toml", "a = 5\n")  # already changed
    inst = _Inst("rz-ae", repo, "codex")
    mon.tick({"rz-ae": inst}, {})
    _write(repo, "app.py", "print(99)\n")
    mon.tick({"rz-ae": inst}, {})  # throttled: not diffed yet
    assert _events_of(got, "session.red_zone_breached") == []
    red_zones.add_zone("repo", rid, "config/")
    mon.tick({"rz-ae": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_breached")
    assert ev["data"]["paths"] == ["app.py"]  # config/ was absorbed silently
    assert mon.summary("rz-ae")["breaches"] == 2


# --------------------------------------------------------------------------- #
# Breach wording: what actually blocks a push
# --------------------------------------------------------------------------- #
def test_breach_detail_says_what_it_blocks(repo, got):
    """F47: the push gate reads COMMITTED changes only, and an ignored file
    never leaves the machine."""
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-af", repo, "bash")
    mon.tick({"rz-af": inst}, {})
    _write(repo, "config/a.toml", "x = 1\n")
    mon.tick({"rz-af": inst}, {})
    _write(repo, "config/b.toml", "x = 1\n")
    _git(repo, "add", "config/b.toml")
    _git(repo, "commit", "-qm", "b")
    mon.tick({"rz-af": inst}, {})
    first, second = _events_of(got, "session.red_zone_breached")
    assert first["data"]["blocks_push"] is False
    assert first["data"]["detail"].endswith(
        "— committing it would block pushes, PRs and merges"
    )
    assert second["data"]["paths"] == ["config/b.toml"]
    assert second["data"]["blocks_push"] is True
    assert second["data"]["detail"].endswith(
        "— pushing is blocked until it is reverted"
    )


# --------------------------------------------------------------------------- #
# Hooks file that won't parse
# --------------------------------------------------------------------------- #
def test_an_unparsable_hooks_file_is_never_rewritten_or_called_tampering(repo, got):
    """F21: a user saving settings.local.json with a trailing comma must keep
    their file byte-for-byte; the pill says why the guard is off."""
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-ag", repo, "claude")
    mon.tick({"rz-ag": inst}, {})
    assert am.hooks_armed(_settings(repo))
    data = json.loads(_settings(repo).read_text())
    data["permissions"] = {"allow": ["Bash(ls:*)"]}
    data["env"] = {"FOO": "1"}
    good = json.dumps(data, indent=2)
    broken = good[:-2] + ",\n}"  # trailing comma
    _settings(repo).write_text(broken)
    mon.tick({"rz-ag": inst}, {})
    mon.tick({"rz-ag": inst}, {})
    assert _settings(repo).read_text() == broken
    assert _events_of(got, "session.red_zone_tampered") == []
    assert mon.summary("rz-ag")["guard"] == "off"
    info = mon.guard_info("rz-ag", inst, 1)
    assert info["state"] == "off"
    assert info["detail"].startswith("hooks file is not valid JSON")
    assert ".claude/settings.local.json" in info["detail"]
    # Fixed by the user (hooks intact): guarded again, silently.
    _settings(repo).write_text(good)
    mon.tick({"rz-ag": inst}, {})
    assert mon.summary("rz-ag")["guard"] == "guarded"
    assert _events_of(got, "session.red_zone_tampered") == []
    assert json.loads(_settings(repo).read_text())["permissions"]


# --------------------------------------------------------------------------- #
# Push denies are blocks too
# --------------------------------------------------------------------------- #
def _push_deny(ts: float, path: str, tool: str = "Bash") -> dict:
    return {
        "v": 1,
        "ts": ts,
        "ev": "pre",
        "tool": tool,
        "kind": "bash" if tool == "Bash" else "mcp_write",
        "id": "p%s" % ts,
        "cmd": "git push origin feature",
        "deny": {
            "path": path,
            "pattern": None,
            "name": "",
            "zone_id": None,
            "reason": "red zone files are changed on this branch",
            "push": True,
        },
    }


def test_a_refused_push_is_a_block_keyed_apart_from_zone_edits(repo, got):
    """F31: the hook now writes a deny record for a refused push; the monitor
    counts it (last_block_ts, a blocked event) and words it as a push."""
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-ah", repo, "bash")
    mon.tick({"rz-ah": inst}, {"rz-ah": "working"})
    t = time.time()
    _feed(
        "rz-ah",
        _deny(t, "rz_1", "config/", "config/settings.toml"),
        _push_deny(t + 0.01, "config/settings.toml"),
        _push_deny(t + 0.02, "config/settings.toml", tool="mcp__github__push_files"),
    )
    mon.tick({"rz-ah": inst}, {"rz-ah": "working"})
    edit, push = _events_of(got, "session.red_zone_blocked")
    assert edit["data"]["detail"] == "blocked an edit to config/"
    assert edit["data"]["push"] is False
    assert push["data"]["push"] is True
    assert push["data"]["count"] == 2
    assert push["data"]["detail"] == (
        "blocked 2 pushes (zone breaches committed on this branch)"
    )
    assert push["data"]["zone_ids"] == [] and push["data"]["patterns"] == []
    assert push["data"]["paths"] == ["config/settings.toml"]
    assert mon.summary("rz-ah")["last_block_ts"] == pytest.approx(t + 0.02)
    # Once per work cycle, like a zone block.
    _feed("rz-ah", _push_deny(t + 1, "config/settings.toml"))
    mon.tick({"rz-ah": inst}, {"rz-ah": "working"})
    assert len(_events_of(got, "session.red_zone_blocked")) == 2


# --------------------------------------------------------------------------- #
# Case-insensitive filesystems
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ci", [True, False])
def test_a_case_variant_zone_matches_on_a_case_insensitive_fs(
    repo, got, monkeypatch, ci
):
    """F33: on macOS/Windows the hook blocks `Config/` for config/…; the
    monitor must see the same zone reach the same files."""
    monkeypatch.setattr(red_zones, "_probe_case_insensitive", lambda root: ci)
    red_zones.add_zone("repo", _repo_id(repo), "Config/")
    inst = _Inst("rz-ai", repo, "bash")
    mon.tick({"rz-ai": inst}, {})
    _write(repo, "config/settings.toml", "a = 3\n")
    mon.tick({"rz-ai": inst}, {})
    evs = _events_of(got, "session.red_zone_breached")
    assert [e["data"]["paths"] for e in evs] == (
        [["config/settings.toml"]] if ci else []
    )


# --------------------------------------------------------------------------- #
# Guard pill sentences
# --------------------------------------------------------------------------- #
def test_guard_detail_is_an_explanation_not_the_label(repo):
    """F35/F38: the Map shows `detail` as the pill's tooltip."""
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-aj", repo, "claude")
    mon.tick({"rz-aj": inst}, {})
    d = mon.guard_info("rz-aj", inst, 1)["detail"]
    assert d.startswith("Claude Code is blocked before it edits a red zone (hook ")
    _feed("rz-aj", {"v": 1, "ts": time.time(), "ev": "pre", "tool": "Read"})
    mon.tick({"rz-aj": inst}, {})
    d = mon.guard_info("rz-aj", inst, 1)["detail"]
    assert (
        d
        == "Claude Code is blocked before it edits a red zone (hook verified just now)"
    )
    now = 1_000_000.0
    assert mon.guard_detail(
        "arming", "Claude Code", {"armed_at": now - 120}, 0.0, now
    ).startswith("Claude Code hasn't run the red-zone hook yet (armed 2 min ago)")
    assert mon.guard_detail("none", "x").startswith("No red zones")
    # The pre-first-tick guess reads as a sentence too.
    fresh = _Inst("rz-ak", repo, "claude")
    assert mon.guard_info("rz-ak", fresh, 1)["detail"].startswith(
        "Claude Code hasn't run the red-zone hook yet"
    )


# --------------------------------------------------------------------------- #
# forget / after_kill / feed GC
# --------------------------------------------------------------------------- #
def test_forget_keeps_the_zones_of_a_folder_that_survives(repo, tmp_path):
    """F44: an in-place session's folder is the user's checkout — deleting
    the session must not delete its "This worktree" zones. The zones go only
    once the folder is really gone."""
    red_zones.add_zone("worktree", str(repo), "app.py", repo_id=_repo_id(repo))
    inst = _Inst("rz-al", repo, "bash")
    mon.tick({"rz-al": inst}, {})
    gp = Path(red_zones.guard_path(str(repo)))
    mon.forget("rz-al", str(repo), False, worktree_removed=False, inst=inst)
    assert not gp.exists()  # the guard still goes
    mon.after_kill("rz-al", str(repo), False)  # the folder is still there
    assert [z["pattern"] for z in red_zones.worktree_zones(str(repo))] == ["app.py"]
    # A removed worktree takes its zones with it.
    gone = tmp_path / "gone-wt"
    gone.mkdir()
    red_zones.add_zone("worktree", str(gone), "x.py", repo_id="path:" + str(gone))
    mon.after_kill("rz-al", str(gone), False)
    assert red_zones.worktree_zones(str(gone))  # still on disk: kept
    gone.rmdir()
    mon.after_kill("rz-al", str(gone), True)  # shared: kept
    assert red_zones.worktree_zones(str(gone))
    mon.after_kill("rz-al", str(gone), False)
    assert red_zones.worktree_zones(str(gone)) == []


def test_a_delete_landing_mid_tick_cannot_resurrect_the_guard(repo, got, monkeypatch):
    """F45: forget() runs on another thread while a tick is inside
    _tick_root; that tick must not rewrite the guard nor read its own
    restore as tampering, and later ticks (Kill still running, the title
    still registered) must leave the title alone."""
    red_zones.add_zone("repo", _repo_id(repo), "config/")
    inst = _Inst("rz-am", repo, "bash")
    mon.tick({"rz-am": inst}, {})
    gp = Path(red_zones.guard_path(str(repo)))
    assert gp.exists()
    real = mon._breach_pass
    fired = []

    def _then_delete(*a, **k):
        real(*a, **k)
        if not fired:
            fired.append(1)
            mon.forget("rz-am", str(repo), False, worktree_removed=False, inst=inst)

    monkeypatch.setattr(mon, "_breach_pass", _then_delete)
    monkeypatch.setattr(mon, "GUARD_REFRESH_S", 0.0)  # force the guard pass
    mon.tick({"rz-am": inst}, {})
    assert fired and not gp.exists()
    assert _events_of(got, "session.red_zone_tampered") == []
    monkeypatch.setattr(mon, "_breach_pass", real)
    mon.tick({"rz-am": inst}, {})  # Kill still running: still registered
    assert not gp.exists() and mon.summary("rz-am") is None
    assert _events_of(got, "session.red_zone_tampered") == []
    # The title reused by a NEW session is watched again.
    fresh = _Inst("rz-am", repo, "bash")
    mon.tick({"rz-am": fresh}, {})
    assert gp.exists() and mon.summary("rz-am")["zones"] == 1


def test_orphan_feeds_are_trimmed_then_deleted(repo, monkeypatch):
    """F48: feeds no registered session owns (a manual `claude` in a checkout
    that keeps MindFlock's hooks, the `_sh` shell pane) are bounded too."""
    from backend.session import tmux

    fdir = Path(red_zones.feed_dir())
    fdir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"v": 1, "ts": 1.0, "ev": "pre", "tool": "Read", "pad": "x" * 200}
    )
    big = (line + "\n") * 11000  # > 2 MB
    old = time.time() - mon.ORPHAN_FEED_MAX_AGE_S - 60
    orphan_old = fdir / "my-manual-tmux.jsonl"
    orphan_old.write_text(line + "\n")
    os.utime(orphan_old, (old, old))
    orphan_big = fdir / (tmux.to_mindflock_tmux_name("rz-an") + "_sh.jsonl")
    orphan_big.write_text(big)
    paused = Path(red_zones.feed_path(tmux.to_mindflock_tmux_name("rz-paused")))
    paused.write_text(line + "\n")
    os.utime(paused, (old, old))
    inst = _Inst("rz-an", repo, "bash")
    p_inst = _Inst("rz-paused", repo, "bash")
    from backend.session.storage import Paused

    p_inst.Status = Paused
    mon._HK["at"] = 0.0
    mon.tick({"rz-an": inst, "rz-paused": p_inst}, {})
    assert not orphan_old.exists()  # quiet a day, nobody owns it
    assert orphan_big.exists() and orphan_big.stat().st_size < 600_000  # trimmed
    assert paused.exists()  # a paused session's feed is kept


def test_after_kill_removes_a_feed_the_dying_agent_recreated(repo):
    from backend.session import tmux

    feed = Path(red_zones.feed_path(tmux.to_mindflock_tmux_name("rz-ao")))
    feed.parent.mkdir(parents=True, exist_ok=True)
    mon.forget("rz-ao", str(repo), True)
    feed.write_text('{"v":1,"ts":1}\n')  # a Post hook between forget and Kill
    mon.after_kill("rz-ao", str(repo), True)
    assert not feed.exists()


# --------------------------------------------------------------------------- #
# v3 green zones (critic findings named per test)
# --------------------------------------------------------------------------- #
def _green(repo: Path, pattern: str) -> dict:
    return red_zones.add_zone(
        "worktree",
        os.path.realpath(str(repo)),
        pattern,
        repo_id=_repo_id(repo),
        kind="green",
    )


def test_a_stale_v1_tool_hook_is_healed_silently_on_first_sight(repo, got):
    """C1: a hooks file still carrying an older guard (another source hash)
    is not armed → reinstalled with the current one; on first sight that is
    an upgrade, not tampering."""
    _green(repo, "src")
    inst = _Inst("rz-g0", repo, "claude")
    mon.tick({"rz-g0": inst}, {})
    body = _settings(repo).read_text()
    _settings(repo).write_text(
        body.replace(am.TOOL_HOOK_TAG, "# mindflock-activity tool-hook v1")
    )
    mon.reset_for_tests()  # a freshly (re)started server
    mon.tick({"rz-g0": inst}, {})
    assert am.TOOL_HOOK_TAG in _settings(repo).read_text()
    assert am.hooks_armed(_settings(repo))
    assert _events_of(got, "session.red_zone_tampered") == []


def test_a_green_only_worktree_is_enforcing(repo, got):
    """C2: green alone arms the hooks, protects the control files in the
    guard, and heals a disarm (the v2 gates all read `rules`)."""
    _green(repo, "src")
    inst = _Inst("rz-g1", repo, "claude")
    mon.tick({"rz-g1": inst}, {})
    g = json.load(open(red_zones.guard_path(str(repo))))
    assert g["green_rules"] and g["protect"] and g["rules"] == []
    _settings(repo).write_text("{}")
    mon.tick({"rz-g1": inst}, {})
    assert am.hooks_armed(_settings(repo))
    (ev,) = _events_of(got, "session.red_zone_tampered")
    assert ev["data"]["what"] == "hooks"
    s = mon.summary("rz-g1")
    assert s["mode"] == "green" and s["zones"] == 1
    info = mon.guard_info("rz-g1", inst, 1)
    assert info["mode"] == "green"
    assert "outside its green zone(s)" in info["detail"]


def test_green_breach_is_a_change_outside_the_scope(repo, got):
    _green(repo, "src")
    inst = _Inst("rz-g2", repo, "bash")
    mon.tick({"rz-g2": inst}, {})
    _write(repo, "src/new.py", "x\n")  # inside: fine
    _write(repo, "uv.lock", "lock\n")  # companion: fine
    _write(repo, ".mindflock_verify.json", "{}")  # workspace artifact
    mon.tick({"rz-g2": inst}, {})
    assert _events_of(got, "session.red_zone_breached") == []
    _write(repo, "app.py", "print(9)\n")
    mon.tick({"rz-g2": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_breached")
    d = ev["data"]
    assert d["paths"] == ["app.py"] and d["kind"] == "green"
    assert d["patterns"] == ["outside green"]
    assert "outside the green zone(s)" in d["detail"]
    assert mon.summary("rz-g2")["breaches"] == 1


def test_exempt_work_is_not_a_breach_until_it_changes_again(repo, got):
    """C5: work done before the scope existed is exempt at its recorded
    content; editing it afterwards is a breach (and reaches the push gate
    through the guard's committed breaches)."""
    inst = _Inst("rz-g3", repo, "bash")
    _write(repo, "app.py", "print(2)\n")
    _git(repo, "commit", "-qam", "before scope")
    mon.tick({"rz-g3": inst}, {})
    _green(repo, "src")
    blobs = red_zones.worktree_blobs(os.path.realpath(str(repo)), ["app.py"])
    red_zones.set_green_exempt(str(repo), blobs)
    mon.tick({"rz-g3": inst}, {})
    assert _events_of(got, "session.red_zone_breached") == []
    assert mon.summary("rz-g3")["breaches"] == 0
    assert mon.committed_for(str(repo)) == []
    _write(repo, "app.py", "print(3)\n")
    _git(repo, "commit", "-qam", "after scope")
    mon.tick({"rz-g3": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_breached")
    assert ev["data"]["paths"] == ["app.py"] and ev["data"]["blocks_push"] is True
    assert mon.committed_for(str(repo)) == ["app.py"]
    g = json.load(open(red_zones.guard_path(str(repo))))
    assert g["breaches"] == ["app.py"]


def test_reseed_compares_breach_sets_not_old_rules(repo, got):
    """H1: v2 reseed said "matched by an OLD rule ⇒ was already a breach",
    which is backwards for green (an old green rule ALLOWED the path). A
    scope narrowed under finished work absorbs it silently; a genuinely
    pending breach of the old document is still announced."""
    inst = _Inst("rz-g4", repo, "bash")
    _green(repo, "src")
    extra = _green(repo, "docs")
    mon.tick({"rz-g4": inst}, {})
    _write(repo, "docs/guide.md", "done\n")  # allowed while `docs` is green
    mon.tick({"rz-g4": inst}, {})
    red_zones.remove_zone(extra["id"])  # narrowed (no route: no exemption)
    mon.tick({"rz-g4": inst}, {})
    assert _events_of(got, "session.red_zone_breached") == []
    assert mon.summary("rz-g4")["breaches"] == 1  # still listed, not news
    # A pending breach (made while the old scope stood) survives a change.
    _write(repo, "app.py", "print(7)\n")
    red_zones.add_zone("repo", _repo_id(repo), "unrelated/")
    mon.tick({"rz-g4": inst}, {})
    (ev,) = _events_of(got, "session.red_zone_breached")
    assert ev["data"]["paths"] == ["app.py"]


def test_green_deny_records_are_one_scope_request_per_cycle(repo, got):
    _green(repo, "src")
    inst = _Inst("rz-g5", repo, "bash")
    mon.tick({"rz-g5": inst}, {"rz-g5": "working"})
    t = time.time()
    for i, path in enumerate(["app.py", "config/settings.toml"]):
        rec = _deny(t + i, None, "outside green", path)
        rec["deny"].update(kind="green", request=True)
        _feed("rz-g5", rec)
    mon.tick({"rz-g5": inst}, {"rz-g5": "working"})
    (ev,) = _events_of(got, "session.red_zone_blocked")
    d = ev["data"]
    assert d["kind"] == "green" and d["count"] == 2
    assert d["paths"] == ["app.py", "config/settings.toml"]
    assert d["detail"].startswith("blocked 2 edits outside the green zone(s): ")


class _HookProv:
    """A hot-reload provider whose install is the real Claude hook merge."""

    def red_zone_guard(self):
        return True

    def hooks_hot_reload(self):
        return True

    def install_activity_hooks(self, wt, tmux):
        from backend.providers import claude

        claude.install_activity_hooks(wt, tmux)


def _two_builds(monkeypatch, repo, rev2):
    """Alternate ``_hooks_pass`` between this build and another one (its own
    per-process state, tag ``v2.<rev2> deadbeef``), 2 s apart, 20 ticks.
    Returns ``(settings writes, tamper events seen)``."""
    it = {"title": "t", "wt": str(repo), "prov": _HookProv(), "tmux": "t"}
    me = (am.TOOL_HOOK_TAG, am.TOOL_HOOK_REV)
    other = ("%s v2.%d deadbeef" % (am.TOOL_HOOK_TAG_PREFIX, rev2), rev2)
    tampers = []
    monkeypatch.setattr(mon, "TAMPER_COOLDOWN_S", 0.0, raising=False)
    monkeypatch.setattr(mon, "_tampered", lambda title, what, now: tampers.append(what))
    states = {
        b: {"armed": None, "heal_at": 0.0, "dead": False, "first_title": "t"}
        for b in (0, 1)
    }
    path = _settings(repo)
    writes, now = 0, 1000.0
    for _step in range(10):
        for b, (tag, rev) in enumerate((me, other)):
            monkeypatch.setattr(am, "TOOL_HOOK_TAG", tag)
            monkeypatch.setattr(am, "TOOL_HOOK_REV", rev)
            before = path.read_text() if path.exists() else None
            mon._hooks_pass(states[b], [it], False, now)
            writes += (path.read_text() if path.exists() else None) != before
            now += 2.0
    monkeypatch.setattr(am, "TOOL_HOOK_TAG", me[0])
    monkeypatch.setattr(am, "TOOL_HOOK_REV", me[1])
    return writes, tampers


def test_two_builds_on_one_worktree_converge_on_the_newer_guard(repo, monkeypatch):
    """The hash-only stamp made the owner's uv-tool copy and a dev server
    each call the other's hook stale: settings.local.json rewritten every
    tick and a 'guard tampered' alert a minute. A NEWER revision is armed
    for the older build; the newer one replaces an older hook once."""
    writes, tampers = _two_builds(monkeypatch, repo, am.TOOL_HOOK_REV + 1)
    assert tampers == []
    assert writes <= 2
    assert "v2.%d deadbeef" % (am.TOOL_HOOK_REV + 1) in _settings(repo).read_text()


def test_two_same_revision_builds_never_alert_and_heal_at_the_retry_pace(
    repo, monkeypatch
):
    writes, tampers = _two_builds(monkeypatch, repo, am.TOOL_HOOK_REV)
    assert tampers == []
    assert writes <= 4  # 40 s at a 30 s retry pace, not 20 rewrites


def test_a_removed_hook_is_still_tampering(repo, monkeypatch):
    it = {"title": "t", "wt": str(repo), "prov": _HookProv(), "tmux": "t"}
    tampers = []
    monkeypatch.setattr(mon, "_tampered", lambda title, what, now: tampers.append(what))
    rs = {"armed": None, "heal_at": 0.0, "dead": False, "first_title": "t"}
    mon._hooks_pass(rs, [it], True, 1000.0)
    assert rs["armed"] is True and tampers == []
    _settings(repo).write_text("{}")
    mon._hooks_pass(rs, [it], True, 1002.0)
    assert tampers == ["hooks"] and am.hooks_armed(_settings(repo))
