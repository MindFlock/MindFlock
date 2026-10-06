"""Session lineage: ``Parent`` / ``Spawned``, spawn limits, re-parenting, and
the orphan teardown every removal path runs.

Covers the engine fields (Instance <-> InstanceData), the pure helpers in
``backend.web.core.lineage``, the snapshot/pending row keys, the engine's
removal listeners, and the routes: ``POST /api/instances`` (parent / spawned /
limits / base_ref), ``POST /api/instances/{title}/parent``, and the orphaning
wired into DELETE, /close, /cleanup, /api/workspaces/delete, failed starts and
reopen. Route tests replace ``ENGINE.instances`` wholesale (the live engine
reads the real state.json) and stub ``ENGINE.save``.
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import session
from backend.session.instance import (
    FromInstanceData,
    InstanceOptions,
    new_instance,
)
from backend.session.storage import GitWorktreeData, InstanceData, Status
from backend.web import server
from backend.web.core import engine as engine_mod
from backend.web.core import lineage
from backend.web.core import pending as pending_mod
from backend.web.core import snapshot as snapshot_mod
from backend.web.server import app

client = TestClient(app)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
class _Inst:
    """A registry stand-in carrying lineage plus what the routes touch."""

    def __init__(self, title, parent="", spawned=False, wt="", program="bash"):
        self.Title = title
        self.Parent = parent
        self.Spawned = spawned
        self.Path = wt
        self.Program = program
        self.Branch = "b"
        self.Status = Status.Running
        self.InPlace = False
        self.ExtraEnv: dict = {}
        self._wt = wt
        self.calls: list = []

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt

    def SetStatus(self, status):  # noqa: N802
        self.Status = status

    def Kill(self):  # noqa: N802
        self.calls.append("kill")


@pytest.fixture
def reg(monkeypatch):
    """An EMPTY, private registry (never the live state.json's) plus a
    recording ENGINE.save and an event recorder."""
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    saves: list = []
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: saves.append(kw))
    events: list = []
    monkeypatch.setattr(
        server._events.BUS,
        "emit",
        lambda name, **kw: events.append((name, kw)),
    )

    def _close(coro):
        coro.close()

    monkeypatch.setattr(server, "_register_task", _close)

    def add(title, **kw):
        inst = _Inst(title, **kw)
        instances[title] = inst
        return inst

    return SimpleNamespace(instances=instances, saves=saves, events=events, add=add)


# --------------------------------------------------------------------------- #
# Instance fields                                                              #
# --------------------------------------------------------------------------- #
def test_new_instance_carries_lineage(tmp_path):
    inst = new_instance(
        InstanceOptions(title="w", path=str(tmp_path), parent="orch", spawned=True)
    )
    assert inst.Parent == "orch" and inst.Spawned is True
    data = inst.ToInstanceData()
    assert data.parent == "orch" and data.spawned is True


def test_new_instance_defaults_to_a_human_root(tmp_path):
    inst = new_instance(InstanceOptions(title="h", path=str(tmp_path)))
    assert inst.Parent == "" and inst.Spawned is False
    d = inst.ToInstanceData().to_dict()
    assert "parent" not in d and "spawned" not in d


def test_from_instance_data_restores_lineage(tmp_path):
    data = InstanceData(
        title="w",
        path=str(tmp_path),
        program="bash",
        status=Status.Paused,
        parent="orch",
        spawned=True,
        worktree=GitWorktreeData(repo_path=str(tmp_path)),
    )
    inst = FromInstanceData(data, attach=False)
    assert inst.Parent == "orch" and inst.Spawned is True
    back = inst.ToInstanceData()
    assert back.parent == "orch" and back.spawned is True


def test_base_branch_option_is_dropped_without_base_ref(tmp_path):
    inst = new_instance(
        InstanceOptions(title="x", path=str(tmp_path), base_branch="orch")
    )
    assert inst._base_ref == "" and inst._base_branch_override == ""
    inst = new_instance(
        InstanceOptions(
            title="y", path=str(tmp_path), base_ref="abc", base_branch="orch"
        )
    )
    assert inst._base_ref == "abc" and inst._base_branch_override == "orch"


# --------------------------------------------------------------------------- #
# core.lineage helpers                                                         #
# --------------------------------------------------------------------------- #
def _tree():
    """root <- mid <- leaf ; root <- sib ; lone ; dangling -> gone"""
    return {
        "root": _Inst("root"),
        "mid": _Inst("mid", parent="root"),
        "leaf": _Inst("leaf", parent="mid", spawned=True),
        "sib": _Inst("sib", parent="root", spawned=True),
        "lone": _Inst("lone"),
        "dangling": _Inst("dangling", parent="gone"),
    }


def test_live_parent_ignores_dead_and_self_links():
    t = _tree()
    assert lineage.live_parent(t, "mid") == "root"
    assert lineage.live_parent(t, "root") == ""
    assert lineage.live_parent(t, "dangling") == ""
    assert lineage.live_parent(t, "missing") == ""
    t["selfie"] = _Inst("selfie", parent="selfie")
    assert lineage.live_parent(t, "selfie") == ""


def test_children_depth_and_ancestors():
    t = _tree()
    assert lineage.children_of(t, "root") == ["mid", "sib"]
    assert lineage.children_of(t, "leaf") == []
    assert lineage.children_of(t, "") == []
    assert lineage.depth_of(t, "root") == 0
    assert lineage.depth_of(t, "mid") == 1
    assert lineage.depth_of(t, "leaf") == 2
    assert lineage.depth_of(t, "dangling") == 0
    assert lineage.ancestors_of(t, "leaf") == ["mid", "root"]
    assert lineage.ancestors_of(t, "root") == []


def test_walks_terminate_on_a_cycle():
    t = {"a": _Inst("a", parent="b"), "b": _Inst("b", parent="a")}
    assert lineage.depth_of(t, "a") == 1
    assert lineage.ancestors_of(t, "a") == ["b"]


def test_parent_of_tolerates_stand_ins_without_the_field():
    assert lineage.parent_of(SimpleNamespace(Title="x")) == ""
    assert lineage.parent_of(SimpleNamespace(Parent=None)) == ""


def test_parent_error_rules():
    t = _tree()
    assert lineage.parent_error(t, "lone", "root") is None
    assert lineage.parent_error(t, "lone", "lone") == (
        "a session cannot be its own parent"
    )
    assert lineage.parent_error(t, "lone", "nope") == "unknown parent session: nope"
    # root adopting its own grandchild would close a loop.
    err = lineage.parent_error(t, "root", "leaf")
    assert err and "cycle" in err
    # Re-parenting within the tree is fine.
    assert lineage.parent_error(t, "leaf", "root") is None


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 8), ("", 8), ("3", 3), ("0", 0), ("-1", 8), ("x", 8), (" 5 ", 5)],
)
def test_limit_reads_env_per_call(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("MINDFLOCK_MAX_CHILDREN", raising=False)
    else:
        monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", raw)
    assert lineage.limit("MINDFLOCK_MAX_CHILDREN", 8) == expected


def test_spawn_limits_children(monkeypatch):
    t = _tree()
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "2")
    err = lineage.spawn_limit_error(t, "root", False)
    assert err and "MINDFLOCK_MAX_CHILDREN=2" in err and "root" in err
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "3")
    assert lineage.spawn_limit_error(t, "root", False) is None


def test_spawn_limits_depth(monkeypatch):
    t = _tree()
    monkeypatch.setenv("MINDFLOCK_MAX_SPAWN_DEPTH", "2")
    # A child of leaf (depth 2) would be depth 3.
    err = lineage.spawn_limit_error(t, "leaf", False)
    assert err and "depth 3" in err and "MINDFLOCK_MAX_SPAWN_DEPTH=2" in err
    assert lineage.spawn_limit_error(t, "mid", False) is None
    # Default depth 3 allows it.
    monkeypatch.delenv("MINDFLOCK_MAX_SPAWN_DEPTH")
    assert lineage.spawn_limit_error(t, "leaf", False) is None


def test_spawn_limits_total_spawned_applies_with_or_without_parent(monkeypatch):
    t = _tree()  # two live spawned sessions
    monkeypatch.setenv("MINDFLOCK_MAX_SPAWNED", "2")
    err = lineage.spawn_limit_error(t, "", True)
    assert err and "MINDFLOCK_MAX_SPAWNED=2" in err
    assert lineage.spawn_limit_error(t, "lone", True) == err
    # A human-created child (not spawned) is not counted against the cap.
    assert lineage.spawn_limit_error(t, "lone", False) is None
    assert lineage.spawn_limit_error(t, "", False) is None


def test_spawn_limits_defaults():
    assert lineage.spawn_limit_error({}, "", True) is None
    eight = {"p": _Inst("p")}
    for i in range(8):
        eight["c%d" % i] = _Inst("c%d" % i, parent="p")
    err = lineage.spawn_limit_error(eight, "p", False)
    assert err and "MINDFLOCK_MAX_CHILDREN=8" in err


# --- the caps as settings (Settings → Agent orchestration) --------------------
def _cap_settings(monkeypatch, **general):
    from backend.config import settings as S

    for env in (
        "MINDFLOCK_MAX_CHILDREN",
        "MINDFLOCK_MAX_SPAWN_DEPTH",
        "MINDFLOCK_MAX_SPAWNED",
    ):
        monkeypatch.delenv(env, raising=False)
    S.update_settings(general=general)


def test_limit_reads_the_setting_when_no_env(monkeypatch):
    _cap_settings(monkeypatch, agent_max_children=3, agent_max_spawned=40)
    assert lineage.limit("MINDFLOCK_MAX_CHILDREN", 8) == 3
    assert lineage.limit("MINDFLOCK_MAX_SPAWNED", 24) == 40
    # Unset field: the built-in default.
    assert lineage.limit("MINDFLOCK_MAX_SPAWN_DEPTH", 3) == 3
    assert lineage.source("MINDFLOCK_MAX_CHILDREN") == "settings"
    assert lineage.source("MINDFLOCK_MAX_SPAWN_DEPTH") == "default"


def test_env_still_overrides_the_setting(monkeypatch):
    _cap_settings(monkeypatch, agent_max_children=3)
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "5")
    assert lineage.limit("MINDFLOCK_MAX_CHILDREN", 8) == 5
    assert lineage.source("MINDFLOCK_MAX_CHILDREN") == "env"
    # A bad env value falls through to the setting, not to "no limit".
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "lots")
    assert lineage.limit("MINDFLOCK_MAX_CHILDREN", 8) == 3


def test_a_raised_setting_lets_a_ninth_child_spawn(monkeypatch):
    eight = {"p": _Inst("p")}
    for i in range(8):
        eight["c%d" % i] = _Inst("c%d" % i, parent="p")
    _cap_settings(monkeypatch)
    err = lineage.spawn_limit_error(eight, "p", False)
    # The default refusal names both the env knob and where to raise it.
    assert "MINDFLOCK_MAX_CHILDREN=8" in err and "Settings → Agent orchestration" in err
    _cap_settings(monkeypatch, agent_max_children=12)
    assert lineage.spawn_limit_error(eight, "p", False) is None
    _cap_settings(monkeypatch, agent_max_children=2)
    err = lineage.spawn_limit_error(eight, "p", False)
    assert "limit 2, set in Settings → Agent orchestration" in err
    assert "MINDFLOCK_MAX_CHILDREN" not in err


def test_settings_round_trip_the_caps_and_drop_negatives():
    from backend.config.settings import GeneralSettings

    g = GeneralSettings.from_dict(
        {
            "agent_max_children": "12",
            "agent_max_spawn_depth": -1,
            "agent_max_spawned": 0,
        }
    )
    assert (g.agent_max_children, g.agent_max_spawn_depth, g.agent_max_spawned) == (
        12,
        None,
        0,
    )
    assert g.to_dict() == {"agent_max_children": 12, "agent_max_spawned": 0}


# --- base_ref validation against a real repo ---------------------------------
def _git(cwd, *args):
    res = subprocess.run(
        ["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    assert res.returncode == 0, res.stdout
    return res.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "a")
    _git(path, "switch", "-q", "-c", "orch")
    (path / "b.txt").write_text("b\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "b")
    orch = _git(path, "rev-parse", "HEAD")
    _git(path, "switch", "-q", "main")
    return SimpleNamespace(path=str(path), orch=orch)


def test_resolve_base_ref(repo):
    assert lineage.resolve_base_ref(repo.path, "orch") == repo.orch
    assert lineage.resolve_base_ref(repo.path, repo.orch[:8]) == repo.orch
    assert lineage.resolve_base_ref(repo.path, "nope") == ""
    assert lineage.resolve_base_ref(repo.path, "--all") == ""
    assert lineage.resolve_base_ref(repo.path + "-missing", "orch") == ""


def test_base_ref_error(repo):
    assert lineage.base_ref_error(repo.path, "", "") is None
    assert lineage.base_ref_error(repo.path, "orch", "") is None
    assert lineage.base_ref_error(repo.path, repo.orch, "orch") is None
    # base_branch need not exist locally (it may only live on a remote).
    assert lineage.base_ref_error(repo.path, repo.orch, "feature/remote-only") is None
    assert lineage.base_ref_error(repo.path, "", "orch") == (
        "base_branch requires base_ref"
    )
    assert "unknown base_ref: nope" in lineage.base_ref_error(repo.path, "nope", "")
    assert "invalid base_ref" in lineage.base_ref_error(repo.path, "-x", "")
    assert "invalid base_ref" in lineage.base_ref_error(repo.path, "a b", "")
    assert "invalid base_ref" in lineage.base_ref_error(repo.path, "a" * 300, "")
    assert "invalid base_branch" in lineage.base_ref_error(
        repo.path, "orch", "bad..name"
    )
    assert "invalid base_branch" in lineage.base_ref_error(repo.path, "orch", "-b")


# --------------------------------------------------------------------------- #
# Rows                                                                         #
# --------------------------------------------------------------------------- #
def test_live_parent_in_rows_is_lazy(reg):
    reg.add("orch")
    kid = reg.add("kid", parent="orch", spawned=True)
    orphan = reg.add("orphan", parent="gone")
    assert snapshot_mod._live_parent(kid) == "orch"
    assert snapshot_mod._live_parent(orphan) == ""
    # The stored claim is untouched — only the row refuses to vouch for it.
    assert orphan.Parent == "gone"
    kid.Parent = "kid"
    assert snapshot_mod._live_parent(kid) == ""


def test_instance_json_exposes_parent_and_spawned(reg, tmp_path):
    reg.add("orch")
    inst = new_instance(
        InstanceOptions(
            title="kid", path=str(tmp_path), program="bash", parent="orch", spawned=True
        )
    )
    reg.instances["kid"] = inst
    row = snapshot_mod._instance_json(inst, cheap=True)
    assert row["parent"] == "orch" and row["spawned"] is True
    reg.instances.pop("orch")
    row = snapshot_mod._instance_json(inst, cheap=True)
    assert row["parent"] == "" and row["spawned"] is True


def test_pending_rows_carry_lineage_defaults(monkeypatch):
    monkeypatch.setattr(
        pending_mod, "snapshot", lambda: {"sc-1": {"branch": "feature/sc-1/x"}}
    )
    eng = SimpleNamespace(instances={}, default_program=lambda: "claude")
    (row,) = pending_mod.rows(eng)
    assert row["parent"] == "" and row["spawned"] is False


# --------------------------------------------------------------------------- #
# Engine removal listeners                                                     #
# --------------------------------------------------------------------------- #
def _mk_data(title, wt, age=300.0):
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=age)
    return InstanceData(
        title=title,
        path=wt,
        branch="b",
        status=Status.Running,
        created_at=t,
        updated_at=t,
        program="bash",
        worktree=GitWorktreeData(repo_path=wt, worktree_path=wt, session_name=title),
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def test_engine_notifies_listeners_of_convergence_drops(home, tmp_path, monkeypatch):
    wt = tmp_path / "ws"
    wt.mkdir()
    a = engine_mod.Engine()
    b = engine_mod.Engine()
    a.instances["conv"] = FromInstanceData(_mk_data("conv", str(wt)), attach=False)
    a.save()
    b.instances["conv"] = FromInstanceData(_mk_data("conv", str(wt)), attach=False)
    seen: list = []
    b.add_removal_listener(seen.append)
    b.add_removal_listener(seen.append)  # idempotent
    time.sleep(0.01)
    a.instances.pop("conv")
    a.save(exclude_titles={"conv"})
    # A's own route-driven removal is NOT an engine-side drop.
    assert seen == []
    b.save()
    assert "conv" not in b.instances
    assert seen == ["conv"]

    # The adopt/reload sync path notifies too.
    b.instances["conv"] = FromInstanceData(
        _mk_data("conv", str(wt), age=600.0), attach=False
    )
    monkeypatch.setattr(engine_mod, "_ENGINE", b)
    engine_mod._LAST_STATE_SIG[:] = [None, None]
    engine_mod._sync_external_instances()
    assert seen == ["conv", "conv"]
    # ...including the unchanged-file fast path.
    b.instances["conv"] = FromInstanceData(
        _mk_data("conv", str(wt), age=600.0), attach=False
    )
    engine_mod._sync_external_instances()
    assert seen == ["conv", "conv", "conv"]


def test_engine_listener_failure_never_breaks_the_engine(home):
    eng = engine_mod.Engine()
    calls: list = []

    def boom(title):
        raise RuntimeError("nope")

    eng.add_removal_listener(boom)
    eng.add_removal_listener(calls.append)
    eng._notify_removed(["x", "y"])
    assert calls == ["x", "y"]
    eng.remove_removal_listener(boom)
    eng.remove_removal_listener(boom)  # absent: no-op
    assert eng.removal_listeners == [calls.append]


def test_server_registers_its_teardown_on_the_live_engine():
    assert server._on_session_removed in server.ENGINE.removal_listeners


# --------------------------------------------------------------------------- #
# Orphan teardown                                                              #
# --------------------------------------------------------------------------- #
def test_orphan_children_clears_and_saves(reg):
    reg.add("orch")
    a = reg.add("a", parent="orch")
    b = reg.add("b", parent="orch", spawned=True)
    c = reg.add("c", parent="other")
    assert sorted(server._orphan_children("orch")) == ["a", "b"]
    assert a.Parent == "" and b.Parent == "" and c.Parent == "other"
    # Spawned is history, not a link: untouched.
    assert b.Spawned is True
    assert reg.saves == [{}]
    # Nothing to do -> no save.
    assert server._orphan_children("orch") == []
    assert server._orphan_children("") == []
    assert reg.saves == [{}]


def test_orphan_dangling_parents_sweep(reg):
    reg.add("orch")
    ok = reg.add("ok", parent="orch")
    dead = reg.add("dead", parent="gone")
    selfie = reg.add("selfie", parent="selfie")
    reg.add("root")
    assert sorted(server._orphan_dangling_parents()) == ["dead", "selfie"]
    assert ok.Parent == "orch" and dead.Parent == "" and selfie.Parent == ""
    assert len(reg.saves) == 1
    assert server._orphan_dangling_parents() == []
    assert len(reg.saves) == 1


def test_on_session_removed_runs_hooks_in_order_and_isolates_failures(reg, monkeypatch):
    reg.add("kid", parent="orch")
    calls: list = []

    def first(title):
        calls.append(("first", title))
        raise RuntimeError("boom")

    def second(title):
        calls.append(("second", title))

    monkeypatch.setattr(server, "_SESSION_REMOVED_HOOKS", [first, second])
    server._on_session_removed("orch")
    assert calls == [("first", "orch"), ("second", "orch")]
    assert reg.instances["kid"].Parent == ""


def test_on_session_removed_survives_an_orphaning_failure(reg, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        server,
        "_orphan_children",
        lambda t: (_ for _ in ()).throw(RuntimeError("x")),
    )
    monkeypatch.setattr(server, "_SESSION_REMOVED_HOOKS", [calls.append])
    server._on_session_removed("t")
    assert calls == ["t"]


def test_drop_failed_start_runs_the_teardown_only_for_its_own_record(reg, monkeypatch):
    calls: list = []
    monkeypatch.setattr(server, "_SESSION_REMOVED_HOOKS", [calls.append])
    mine = reg.add("t")
    reg.add("kid", parent="t")
    assert server._drop_failed_start("t", mine) is True
    assert calls == ["t"] and "t" not in reg.instances
    assert reg.instances["kid"].Parent == ""
    # A live namesake owns the title: no pop, no teardown.
    reg.add("t")
    reg.add("kid2", parent="t")
    assert server._drop_failed_start("t", mine) is False
    assert calls == ["t"] and reg.instances["kid2"].Parent == "t"
    # Already gone: report it, but there is nothing of ours to tear down.
    reg.instances.pop("t")
    assert server._drop_failed_start("t", mine) is True
    assert calls == ["t"]


# --------------------------------------------------------------------------- #
# Removal routes orphan their children                                         #
# --------------------------------------------------------------------------- #
@pytest.fixture
def _quiet_teardown(monkeypatch):
    for name in (
        "_kill_shell_session",
        "_kill_agent_session",
        "_close_cursor_window",
        "_remove_trust_entry",
        "_record_closed",
    ):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_worktree_in_use_by_other", lambda wt, t: False)
    monkeypatch.setattr(server._red_zone_monitor, "forget", lambda *a, **k: None)
    monkeypatch.setattr(server._red_zone_monitor, "after_kill", lambda *a, **k: None)
    monkeypatch.setattr(server._code_outline, "forget", lambda *a, **k: None)
    monkeypatch.setattr(server._ports, "release", lambda t: None)
    hooks: list = []
    monkeypatch.setattr(server, "_SESSION_REMOVED_HOOKS", [hooks.append])
    return hooks


@pytest.mark.parametrize(
    "method,path",
    [
        ("delete", "/api/instances/orch"),
        ("post", "/api/instances/orch/close"),
        ("post", "/api/instances/orch/cleanup"),
    ],
)
def test_removal_routes_orphan_children(
    reg, _quiet_teardown, monkeypatch, tmp_path, method, path
):
    monkeypatch.setattr(server.shutil, "rmtree", lambda *a, **k: None)
    reg.add("orch", wt=str(tmp_path))
    kid = reg.add("kid", parent="orch", spawned=True)
    other = reg.add("other", parent="else")
    r = getattr(client, method)(path)
    assert r.status_code == 200, r.text
    assert "orch" not in reg.instances
    assert kid.Parent == "" and other.Parent == "else"
    assert _quiet_teardown == ["orch"]


def test_workspace_delete_orphans_the_killed_sessions_children(
    reg, _quiet_teardown, monkeypatch, tmp_path
):
    root = tmp_path / "ws-root"
    ws = root / "orch-ws"
    ws.mkdir(parents=True)
    monkeypatch.setattr(server, "_workspace_roots", lambda: [str(root)])
    monkeypatch.setattr(server, "scan_workspaces", lambda *a, **k: [{"path": str(ws)}])
    orch = reg.add("orch", wt=str(ws))
    kid = reg.add("kid", parent="orch")
    r = client.post("/api/workspaces/delete", json={"path": str(ws)})
    assert r.status_code == 200, r.text
    assert r.json()["killed_session"] == "orch"
    assert "kill" in orch.calls and kid.Parent == ""
    assert _quiet_teardown == ["orch"]


def test_reopen_clears_a_parent_that_is_gone(reg, monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    data = InstanceData(
        title="kid",
        path=str(wt),
        program="bash",
        parent="gone",
        spawned=True,
        worktree=GitWorktreeData(worktree_path=str(wt)),
    ).to_dict()
    entries = [{"id": "e1", "title": "kid", "folder": str(wt), "data": data}]
    monkeypatch.setattr(server, "_load_recently_closed", lambda: list(entries))
    monkeypatch.setattr(server, "_save_recently_closed", lambda items: None)
    monkeypatch.setattr(server, "_ensure_agent_session", lambda i, t: ("n", None))
    monkeypatch.setattr(
        server,
        "_instance_json",
        lambda i, **k: {"title": i.Title, "parent": i.Parent, "spawned": i.Spawned},
    )
    r = client.post("/api/recently-closed/e1/reopen")
    assert r.status_code == 200, r.text
    assert r.json() == {"title": "kid", "parent": "", "spawned": True}


def test_reopen_keeps_a_live_parent(reg, monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    reg.add("orch")
    data = InstanceData(
        title="kid",
        path=str(wt),
        program="bash",
        parent="orch",
        worktree=GitWorktreeData(worktree_path=str(wt)),
    ).to_dict()
    entries = [{"id": "e1", "title": "kid", "folder": str(wt), "data": data}]
    monkeypatch.setattr(server, "_load_recently_closed", lambda: list(entries))
    monkeypatch.setattr(server, "_save_recently_closed", lambda items: None)
    monkeypatch.setattr(server, "_ensure_agent_session", lambda i, t: ("n", None))
    monkeypatch.setattr(server, "_instance_json", lambda i, **k: {"parent": i.Parent})
    r = client.post("/api/recently-closed/e1/reopen")
    assert r.json() == {"parent": "orch"}


# --------------------------------------------------------------------------- #
# POST /api/instances — lineage                                                #
# --------------------------------------------------------------------------- #
class _NewInst:
    def __init__(self, opts):
        self.opts = opts
        self.Title = opts.title
        self.Branch = ""
        self.Program = opts.program
        self.Path = opts.path
        self.Prompt = opts.prompt
        self.Parent = opts.parent
        self.Spawned = opts.spawned
        self.Status = Status.Loading
        self.ExtraEnv = {}

    def SetStatus(self, s):  # noqa: N802
        self.Status = s

    def Started(self):  # noqa: N802
        return False

    def GetWorktreePath(self):  # noqa: N802
        return ""


@pytest.fixture
def create(reg, monkeypatch, tmp_path):
    """create_instance with every side effect stubbed; the real lineage logic
    runs. ``repo_path`` points at a real (non-git) folder by default."""
    captured: list = []

    def _new(opts):
        captured.append(opts)
        return _NewInst(opts)

    monkeypatch.setattr(session, "NewInstance", _new)
    monkeypatch.setattr(
        server,
        "_instance_json",
        lambda i, **k: {"title": i.Title, "parent": i.Parent, "spawned": i.Spawned},
    )
    monkeypatch.setattr(server, "_mark_onboarded", lambda: None)
    monkeypatch.setattr(server._ports, "env_for", lambda t: {})
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    monkeypatch.setattr(
        server, "_prepare_plain_repo", lambda repo, init: (str(tmp_path), True)
    )
    for knob in ("MAX_CHILDREN", "MAX_SPAWN_DEPTH", "MAX_SPAWNED"):
        monkeypatch.delenv("MINDFLOCK_" + knob, raising=False)

    def post(**body):
        body.setdefault("program", "bash")
        body.setdefault("repo_path", str(tmp_path))
        return client.post("/api/instances", json=body)

    return SimpleNamespace(post=post, captured=captured, reg=reg)


def test_create_with_parent_and_spawned(create):
    create.reg.add("orch")
    r = create.post(title="w1", parent="orch", spawned=True)
    assert r.status_code == 202, r.text
    assert r.json() == {
        "title": "w1",
        "parent": "orch",
        "spawned": True,
        "prompt_delivery": "none",
    }
    opts = create.captured[0]
    assert opts.parent == "orch" and opts.spawned is True
    (ev,) = [e for e in create.reg.events if e[0] == "session.created"]
    assert ev[1]["data"]["parent"] == "orch" and ev[1]["data"]["spawned"] is True


def test_create_without_lineage_keeps_the_event_shape(create):
    r = create.post(title="plain")
    assert r.status_code == 202
    (ev,) = [e for e in create.reg.events if e[0] == "session.created"]
    assert set(ev[1]["data"]) == {"program", "provisioned"}
    assert create.captured[0].parent == "" and create.captured[0].spawned is False


def test_create_unknown_parent_is_400(create):
    r = create.post(title="w", parent="ghost")
    assert r.status_code == 400
    assert r.json()["error"] == "unknown parent session: ghost"
    assert "w" not in create.reg.instances


@pytest.mark.parametrize("bad", ["true", 1, [], {}])
def test_create_spawned_must_be_a_boolean(create, bad):
    r = create.post(title="w", spawned=bad)
    assert r.status_code == 400
    assert r.json()["error"] == "spawned must be a boolean"


def test_create_spawned_null_reads_as_false(create):
    assert create.post(title="w", spawned=None).status_code == 202
    assert create.captured[0].spawned is False


def test_create_budget_locked_parent_is_409(create, monkeypatch):
    create.reg.add("orch")
    monkeypatch.setattr(server, "_budget_locked", lambda t: t == "orch")
    r = create.post(title="w", parent="orch")
    assert r.status_code == 409
    assert r.json()["budget_locked"] is True and "orch" in r.json()["error"]


def test_create_children_limit_is_409_and_claims_nothing(create, monkeypatch):
    create.reg.add("orch")
    create.reg.add("c1", parent="orch")
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "1")
    r = create.post(title="w", parent="orch", spawned=True)
    assert r.status_code == 409
    assert "MINDFLOCK_MAX_CHILDREN=1" in r.json()["error"]
    assert "w" not in create.reg.instances
    assert not [e for e in create.reg.events if e[0] == "session.created"]


def test_create_depth_limit_is_409(create, monkeypatch):
    create.reg.add("r")
    create.reg.add("m", parent="r")
    monkeypatch.setenv("MINDFLOCK_MAX_SPAWN_DEPTH", "1")
    r = create.post(title="w", parent="m")
    assert r.status_code == 409
    assert "MINDFLOCK_MAX_SPAWN_DEPTH=1" in r.json()["error"]
    assert create.post(title="w", parent="r").status_code == 202


def test_create_total_spawned_limit_is_409(create, monkeypatch):
    create.reg.add("s1", spawned=True)
    monkeypatch.setenv("MINDFLOCK_MAX_SPAWNED", "1")
    r = create.post(title="w", spawned=True)
    assert r.status_code == 409
    assert "MINDFLOCK_MAX_SPAWNED=1" in r.json()["error"]
    # Human sessions are never capped by it.
    assert create.post(title="h").status_code == 202


def test_create_rechecks_the_parent_under_the_claim(create, monkeypatch):
    """The parent vanishing during the repo-preparation hop is a 400 at claim
    time, not a child of a ghost."""
    create.reg.add("orch")

    def _prep(repo, init):
        create.reg.instances.pop("orch", None)
        return (repo, True)

    monkeypatch.setattr(server, "_prepare_plain_repo", _prep)
    r = create.post(title="w", parent="orch")
    assert r.status_code == 400
    assert r.json()["error"] == "unknown parent session: orch"
    assert "w" not in create.reg.instances


def test_copy_does_not_inherit_lineage(reg, monkeypatch, tmp_path):
    reg.add("orch")
    reg.add("src", parent="orch", spawned=True, wt=str(tmp_path))
    captured: list = []
    monkeypatch.setattr(
        session, "NewInstance", lambda o: captured.append(o) or _NewInst(o)
    )
    monkeypatch.setattr(server, "_instance_json", lambda i, **k: {})
    assert client.post("/api/instances/src/copy").status_code == 202
    assert captured[0].parent == "" and captured[0].spawned is False


# --- base_ref ----------------------------------------------------------------
def test_create_passes_a_valid_base_ref(create, repo, monkeypatch):
    monkeypatch.setattr(server, "_prepare_plain_repo", lambda r, i: (repo.path, True))
    r = create.post(title="w", base_ref=repo.orch, base_branch="orch")
    assert r.status_code == 202, r.text
    opts = create.captured[0]
    assert opts.base_ref == repo.orch and opts.base_branch == "orch"


def test_create_unknown_base_ref_is_400(create, repo, monkeypatch):
    monkeypatch.setattr(server, "_prepare_plain_repo", lambda r, i: (repo.path, True))
    r = create.post(title="w", base_ref="does-not-exist")
    assert r.status_code == 400
    assert "unknown base_ref: does-not-exist" in r.json()["error"]
    assert create.captured == [] and "w" not in create.reg.instances


def test_create_base_branch_without_base_ref_is_400(create, repo, monkeypatch):
    monkeypatch.setattr(server, "_prepare_plain_repo", lambda r, i: (repo.path, True))
    r = create.post(title="w", base_branch="orch")
    assert r.status_code == 400
    assert r.json()["error"] == "base_branch requires base_ref"


def test_create_base_ref_rejected_for_in_place(create, repo, monkeypatch):
    monkeypatch.setattr(server, "_prepare_plain_repo", lambda r, i: (repo.path, True))
    r = create.post(title="w", base_ref="orch", in_place=True)
    assert r.status_code == 400 and "not in-place" in r.json()["error"]
    # A non-git folder is forced in-place, so it is refused the same way.
    monkeypatch.setattr(server, "_prepare_plain_repo", lambda r, i: (repo.path, False))
    r = create.post(title="w2", base_ref="orch")
    assert r.status_code == 400 and "not in-place" in r.json()["error"]


def test_create_base_ref_rejected_for_provisioned(create, repo, monkeypatch):
    monkeypatch.setattr(server, "git_available", lambda: True)
    r = create.post(title="w", base_ref="orch", provisioned=True)
    assert r.status_code == 400 and "not provisioned" in r.json()["error"]


# --------------------------------------------------------------------------- #
# POST /api/instances/{title}/parent                                           #
# --------------------------------------------------------------------------- #
@pytest.fixture
def parent_route(reg, monkeypatch):
    monkeypatch.setattr(
        server, "_instance_json", lambda i, **k: {"title": i.Title, "parent": i.Parent}
    )
    return reg


def test_set_parent_adopts_and_saves(parent_route):
    parent_route.add("orch")
    w = parent_route.add("w", spawned=True)
    r = client.post("/api/instances/w/parent", json={"parent": "orch"})
    assert r.status_code == 200
    assert r.json() == {"title": "w", "parent": "orch"}
    assert w.Parent == "orch" and w.Spawned is True
    assert parent_route.saves == [{}]


def test_set_parent_empty_detaches(parent_route):
    parent_route.add("orch")
    w = parent_route.add("w", parent="orch")
    for body in ({"parent": ""}, {"parent": None}, {}):
        w.Parent = "orch"
        r = client.post("/api/instances/w/parent", json=body)
        assert r.status_code == 200 and w.Parent == ""


def test_set_parent_errors(parent_route):
    parent_route.add("root")
    parent_route.add("mid", parent="root")
    parent_route.add("leaf", parent="mid")
    assert client.post("/api/instances/nope/parent", json={}).status_code == 404
    r = client.post("/api/instances/mid/parent", json={"parent": "ghost"})
    assert r.status_code == 400
    assert r.json()["error"] == "unknown parent session: ghost"
    r = client.post("/api/instances/mid/parent", json={"parent": "mid"})
    assert r.status_code == 400 and "own parent" in r.json()["error"]
    r = client.post("/api/instances/root/parent", json={"parent": "leaf"})
    assert r.status_code == 400 and "cycle" in r.json()["error"]
    r = client.post("/api/instances/mid/parent", json={"parent": 5})
    assert r.status_code == 400 and "string" in r.json()["error"]
    # Nothing changed, nothing saved.
    assert parent_route.instances["mid"].Parent == "root"
    assert parent_route.instances["root"].Parent == ""
    assert parent_route.saves == []


# --------------------------------------------------------------------------- #
# Review fixes: taken branches, create-failure reasons, adopt limits, fresh   #
# lineage on the cached listing                                               #
# --------------------------------------------------------------------------- #
def test_branch_taken_error(repo):
    assert lineage.branch_taken_error(repo.path, "orch") is not None
    assert "already exists" in lineage.branch_taken_error(repo.path, "orch")
    assert lineage.branch_taken_error(repo.path, "free-name") is None
    assert lineage.branch_taken_error(repo.path, "") is None
    assert lineage.branch_taken_error(repo.path, "-x") is None


def test_create_base_ref_with_a_taken_branch_is_409(create, repo, monkeypatch):
    """A closed/paused namesake keeps its branch: refusing it must be a
    synchronous 409 (which the MCP's default-title loop retries past), not a
    202 whose background Start fails — and must leave the branch alone."""
    monkeypatch.setattr(server, "_prepare_plain_repo", lambda r, i: (repo.path, True))
    branch = server._session_branch_name("w1")
    assert branch
    _git(repo.path, "branch", branch, repo.orch)
    r = create.post(title="w1", base_ref=repo.orch)
    assert r.status_code == 409, r.text
    assert "already exists" in r.json()["error"] and branch in r.json()["error"]
    assert create.captured == [] and "w1" not in create.reg.instances
    assert _git(repo.path, "rev-parse", "refs/heads/" + branch) == repo.orch
    # A free title still goes through.
    assert create.post(title="w2", base_ref=repo.orch).status_code == 202


def test_create_failures_are_queryable(monkeypatch):
    monkeypatch.setattr(server, "_CREATE_FAILURES", {})
    server._note_create_failure("w9", "failed to setup git worktree: boom")
    body = client.get("/api/create_failures", params={"title": "w9"}).json()
    assert body["failures"]["w9"]["error"] == "failed to setup git worktree: boom"
    assert client.get("/api/create_failures", params={"title": "x"}).json() == {
        "failures": {}
    }
    # Old entries age out.
    server._CREATE_FAILURES["w9"]["ts"] -= server._CREATE_FAILURE_TTL_S + 1
    assert client.get("/api/create_failures").json() == {"failures": {}}


def test_adopt_limit_error_rules(monkeypatch):
    for knob in ("MAX_CHILDREN", "MAX_SPAWN_DEPTH"):
        monkeypatch.delenv("MINDFLOCK_" + knob, raising=False)
    tree = {
        "orch": _Inst("orch"),
        "w1": _Inst("w1", parent="orch"),
        "o2": _Inst("o2"),
        "o2k": _Inst("o2k", parent="o2"),
        "loose": _Inst("loose"),
    }
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "1")
    err = lineage.adopt_limit_error(tree, "loose", "orch")
    assert err and "MINDFLOCK_MAX_CHILDREN=1" in err
    # Re-asserting an existing child is not a new slot.
    assert lineage.adopt_limit_error(tree, "w1", "orch") is None
    assert lineage.adopt_limit_error(tree, "loose", "") is None
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "8")
    monkeypatch.setenv("MINDFLOCK_MAX_SPAWN_DEPTH", "1")
    # o2 brings a child along: o2k would land at depth 2.
    err = lineage.adopt_limit_error(tree, "o2", "orch")
    assert err and "MINDFLOCK_MAX_SPAWN_DEPTH=1" in err
    assert lineage.adopt_limit_error(tree, "loose", "orch") is None


def test_set_parent_adopt_respects_the_children_cap(parent_route, monkeypatch):
    parent_route.add("orch")
    parent_route.add("w1", parent="orch")
    w2 = parent_route.add("w2", spawned=True)
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "1")
    r = client.post("/api/instances/w2/parent", json={"parent": "orch"})
    assert r.status_code == 409 and "MINDFLOCK_MAX_CHILDREN=1" in r.json()["error"]
    assert w2.Parent == "" and parent_route.saves == []
    # Detaching is never capped.
    r = client.post("/api/instances/w1/parent", json={"parent": ""})
    assert r.status_code == 200


def test_listing_serves_the_live_parent_from_a_fresh_snapshot(
    parent_route, monkeypatch
):
    """A re-parent changes no title, so the tick-snapshot fast path used to
    serve the OLD parent for up to ~10s — a just-detached former parent could
    still pass the MCP's descendant check."""
    parent_route.add("orch2")
    w = parent_route.add("w", parent="orch2")
    monkeypatch.setattr(server._remote, "merged_instances", lambda: [])
    monkeypatch.setattr(server, "_pending_rows", lambda: [])
    server._events.set_sessions_snapshot(
        [{"title": "orch2", "parent": ""}, {"title": "w", "parent": "orch2"}]
    )
    monkeypatch.setattr(server, "_SNAPSHOT_AT", time.time())
    try:
        r = client.post("/api/instances/w/parent", json={"parent": ""})
        assert r.status_code == 200
        assert w.Parent == ""
        rows = {d["title"]: d for d in client.get("/api/instances").json()}
        assert rows["w"]["parent"] == ""
    finally:
        server._events.set_sessions_snapshot([])


def test_instance_json_carries_created_at(reg, tmp_path):
    inst = new_instance(InstanceOptions(title="k", path=str(tmp_path), program="bash"))
    reg.instances["k"] = inst
    row = snapshot_mod._instance_json(inst, cheap=True)
    assert abs(row["created_at"] - inst.CreatedAt.timestamp()) < 1e-6
    inst.CreatedAt = None
    assert snapshot_mod._instance_json(inst, cheap=True)["created_at"] is None


def test_create_extra_launch_args_keep_the_users_defaults(create, monkeypatch):
    """``launch_args`` REPLACES the user's Settings → Coding CLI flags; the
    MCP's spawn sends ``extra_launch_args``, which are appended to them."""
    monkeypatch.setattr(
        server._instance,
        "provider_default_launch_args",
        lambda program: ("--dangerously-skip-permissions",),
    )
    r = create.post(title="w", program="claude", extra_launch_args=["--model", "x"])
    assert r.status_code == 202, r.text
    assert tuple(create.captured[0].launch_args) == (
        "--dangerously-skip-permissions",
        "--model",
        "x",
    )
    r = create.post(title="w2", program="claude", extra_launch_args=["a\nb"])
    assert r.status_code == 400


def test_create_queues_the_prompt_for_a_cli_that_cannot_take_one(create, monkeypatch):
    """A plain session on a CLI with no prompt argument (custom script, aider,
    …) used to launch bare and drop its task."""
    queued = []
    monkeypatch.setattr(
        server._prompt_queue, "enqueue", lambda t, p: queued.append((t, p))
    )
    monkeypatch.setattr(server._prompt_queue, "set_flags", lambda t, **k: None)
    monkeypatch.setattr(server, "_red_zone_prompt", lambda p, *a: p)
    r = create.post(title="g", program="/x/fakeagent.sh", prompt="do the thing")
    assert r.status_code == 202
    assert r.json()["prompt_delivery"] == "queued"
    assert queued == [("g", "do the thing")]
    # A CLI that takes the prompt at launch is seeded as before.
    r = create.post(title="c", program="claude", prompt="do it")
    assert r.json()["prompt_delivery"] == "seeded" and len(queued) == 1


@pytest.mark.parametrize("program", ["bash", "zsh", "/bin/sh -l"])
def test_create_never_queues_a_prompt_for_a_bare_shell(create, monkeypatch, program):
    """A shell resolves to ``generic`` with no prompt argument, so its prompt
    was queued — and the drain typed it, plus Enter, into bash, which ran it
    (the red-zone note's backticked paths as command substitutions)."""
    queued = []
    monkeypatch.setattr(
        server._prompt_queue, "enqueue", lambda t, p: queued.append((t, p))
    )
    monkeypatch.setattr(server._prompt_queue, "set_flags", lambda t, **k: None)
    monkeypatch.setattr(server, "_red_zone_prompt", lambda p, *a: p)
    r = create.post(title="sh1", program=program, prompt="rm `a/`")
    assert r.status_code == 202, r.text
    assert r.json()["prompt_delivery"] != "queued"
    assert queued == []


def test_provider_seeds_prompt_reads_unknown_programs_as_held():
    assert server._provider_seeds_prompt("claude") is True
    assert server._provider_seeds_prompt("bash") is True  # never queued
    assert server._provider_seeds_prompt("aider") is False
    assert server._provider_seeds_prompt("/x/unknown-cli.sh") is False


@pytest.mark.parametrize(
    "defaults,extra,want",
    [
        # A re-set flag keeps BOTH pairs, in order (the CLI's last flag wins) —
        # per-token de-dupe gave ('--model','sonnet','opus'): a stray prompt.
        (
            ("--model", "sonnet"),
            ["--model", "opus"],
            ("--model", "sonnet", "--model", "opus"),
        ),
        (
            ("--permission-mode", "default"),
            ["--permission-mode", "acceptEdits"],
            ("--permission-mode", "default", "--permission-mode", "acceptEdits"),
        ),
        # A shared VALUE never strips the second flag's value.
        (
            ("--model", "opus"),
            ["--fallback-model", "opus"],
            ("--model", "opus", "--fallback-model", "opus"),
        ),
        # A repeated whole group still appears once.
        (
            ("--dangerously-skip-permissions",),
            ["--dangerously-skip-permissions", "--model", "x"],
            ("--dangerously-skip-permissions", "--model", "x"),
        ),
        # Repeatable flags with distinct values survive (codex ``-c k=v``).
        (("-c", "a=1"), ["-c", "b=2"], ("-c", "a=1", "-c", "b=2")),
    ],
)
def test_extra_launch_args_keep_flag_value_pairs_together(
    create, monkeypatch, defaults, extra, want
):
    monkeypatch.setattr(
        server._instance, "provider_default_launch_args", lambda program: defaults
    )
    r = create.post(title="m", program="claude", extra_launch_args=extra)
    assert r.status_code == 202, r.text
    assert tuple(create.captured[-1].launch_args) == want


def test_new_instance_keeps_a_re_set_flag_pair(tmp_path):
    """The engine re-merges the session's args on construction: it must not
    tear the pairs apart a second time."""
    inst = new_instance(
        InstanceOptions(
            title="k2",
            path=str(tmp_path),
            program="claude",
            launch_args=("--model", "sonnet", "--model", "opus", "--model", "opus"),
        )
    )
    assert inst.LaunchArgs == ("--model", "sonnet", "--model", "opus")


def test_provisioned_create_whose_branch_a_closed_session_keeps_is_a_409(
    create, monkeypatch
):
    """A closed provisioned session keeps its worktree (or clone): the create
    answered 202 and Start failed in the background (or silently adopted the
    old clone). Now a synchronous 409 the MCP's retry loop moves past."""
    asked = []

    def taken(strategy, branch, repo="", repo_url_override=None):
        asked.append((strategy, branch))
        return "a worktree for branch %s already exists at /x" % branch

    monkeypatch.setattr(server.provisioning, "provisioned_branch_taken_error", taken)
    r = create.post(title="orch-w1", provisioned=True, workspace_strategy="clone")
    assert r.status_code == 409, r.text
    assert "already exists" in r.json()["error"]
    assert asked == [("clone", "mindflock/orch-w1")]
    assert create.captured == []
    monkeypatch.setattr(
        server.provisioning, "provisioned_branch_taken_error", lambda *a, **k: None
    )
    assert create.post(title="orch-w2", provisioned=True).status_code == 202


def test_provisioned_branch_taken_error_sees_kept_worktrees_and_clones(
    tmp_path, monkeypatch
):
    from backend.session import provisioned as P

    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(
        src,
        "-c",
        "user.email=a@b",
        "-c",
        "user.name=a",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    ws = tmp_path / "ws"
    settings = SimpleNamespace(workspace_dir=ws, repo_url=str(src))
    monkeypatch.setattr(P, "local_settings_for", lambda repo: settings)
    base = P.resolve_base_repo_dir(settings)
    _git(tmp_path, "clone", "-q", str(src), str(base))
    # Worktree strategy: nothing holds the branch yet.
    assert (
        P.provisioned_branch_taken_error("worktree", "mindflock/w1", str(src)) is None
    )
    _git(base, "worktree", "add", "-q", "-b", "mindflock/w1", str(ws / "w1"))
    err = P.provisioned_branch_taken_error("worktree", "mindflock/w1", str(src))
    assert err and "already exists" in err and "w1" in err
    # Clone strategy: the deterministic clone path.
    assert P.provisioned_branch_taken_error("clone", "mindflock/w2", str(src)) is None
    (ws / "mindflock-w2").mkdir(parents=True)
    err = P.provisioned_branch_taken_error("clone", "mindflock/w2", str(src))
    assert err and "already exists" in err
    # A probe that cannot run never blocks a create.
    monkeypatch.setattr(P, "local_settings_for", lambda repo: 1 / 0)
    assert (
        P.provisioned_branch_taken_error("worktree", "mindflock/w1", str(src)) is None
    )
