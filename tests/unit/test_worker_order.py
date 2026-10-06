"""Worker order + orchestrator fences (``core.worker_order``,
``core.worker_order_driver``, the per-session fence in ``red_zones``).

The planner is pure and tested on its own; the driver runs against a fake
server namespace with the REAL prompt queue, zone store and guard files
(``conftest`` points every store at tmp), and the hook end-to-end through the
generated command, as ``test_tool_hook`` does.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.config import red_zones as rz
from backend.providers import activity_markers as am
from backend.web import server
from backend.web.core import mailbox, prompt_queue, team_runs, thread
from backend.web.core import worker_order as wo
from backend.web.core import worker_order_driver as drv


# --------------------------------------------------------------------------- #
# The planner (pure)
# --------------------------------------------------------------------------- #
def _rec(**kw):
    rec = wo._blank_parent(1.0)
    rec.update(kw)
    return rec


def _alive(*titles, report=None, fenced=True):
    return {
        t: {"exists": True, "report": (report or {}).get(t), "fenced": fenced}
        for t in titles
    }


def _apply(rec, acts):
    for kind, t, _d in acts:
        w = rec["workers"][t]
        if kind == "release":
            w["state"] = "running"
            w.pop("start_now", None)
        elif kind in ("done", "stopped"):
            w["state"] = kind
        elif kind == "forget":
            rec["workers"].pop(t)


def test_unordered_workers_release_at_once():
    rec = _rec()
    wo.add_worker(rec, "w1", created=1.0)
    wo.add_worker(rec, "w2", created=1.0)
    acts = wo.plan(rec, _alive("w1", "w2"))
    assert [(k, t) for k, t, _ in acts] == [("release", "w1"), ("release", "w2")]


def test_after_holds_until_the_predecessor_reports_done():
    rec = _rec()
    wo.add_worker(rec, "w1", created=1.0)
    w2 = wo.add_worker(rec, "w2", created=1.0, after=["w1"])
    assert w2["after"] == ["w1"] and w2["why"] == {"w1": "asked"}
    _apply(rec, wo.plan(rec, _alive("w1", "w2")))
    assert rec["workers"]["w1"]["state"] == "running"
    assert rec["workers"]["w2"]["state"] == "held"
    # Still working: nothing moves.
    assert wo.plan(rec, _alive("w1", "w2")) == []
    acts = wo.plan(rec, _alive("w1", "w2", report={"w1": "done"}))
    assert ("done", "w1", "reported done") in acts
    assert ("release", "w2", "") in acts


def test_blocked_frees_its_slot_but_keeps_dependents_held():
    rec = _rec(mode="serial")
    wo.add_worker(rec, "w1", created=1.0)
    wo.add_worker(rec, "w2", created=1.0)  # serial: after w1
    wo.add_worker(rec, "w3", created=1.0, overlaps={})
    assert rec["workers"]["w2"]["after"] == ["w1"]
    assert rec["workers"]["w3"]["after"] == ["w2"]
    _apply(rec, wo.plan(rec, _alive("w1", "w2", "w3")))
    _apply(rec, wo.plan(rec, _alive("w1", "w2", "w3", report={"w1": "blocked"})))
    assert rec["workers"]["w1"]["state"] == "stopped"
    assert rec["workers"]["w2"]["state"] == "held"
    view = wo.view(rec, _alive("w1", "w2", "w3"))
    w2 = view["steps"][1]["workers"][0]
    assert w2["word"] == "waiting" and "w1 (stopped" in w2["detail"]
    # The orchestrator starts it anyway.
    assert wo.set_policy(rec, start_now=["w2"]) == []
    acts = wo.plan(rec, _alive("w1", "w2", "w3"))
    assert ("release", "w2", "started by hand") in acts


def test_cap_releases_oldest_first_as_slots_free():
    rec = _rec(max_parallel=2)
    for t in ("a", "b", "c", "d"):
        wo.add_worker(rec, t, created=1.0)
    _apply(rec, wo.plan(rec, _alive("a", "b", "c", "d")))
    assert [t for t, w in rec["workers"].items() if w["state"] == "running"] == [
        "a",
        "b",
    ]
    view = wo.view(rec, _alive("a", "b", "c", "d"))
    c = [w for w in view["steps"][0]["workers"] if w["title"] == "c"][0]
    assert c["detail"] == "for a free slot (2 at a time)"
    _apply(rec, wo.plan(rec, _alive("a", "b", "c", "d", report={"b": "failed"})))
    assert rec["workers"]["c"]["state"] == "running"
    assert rec["workers"]["d"]["state"] == "held"


def test_a_pending_fence_holds_the_release():
    rec = _rec()
    wo.add_worker(rec, "w1", created=1.0, fence={"only": ["a/**"]})
    assert wo.plan(rec, _alive("w1", fenced=False)) == []
    assert wo.plan(rec, _alive("w1")) == [("release", "w1", "")]


def test_gone_workers():
    rec = _rec()
    wo.add_worker(rec, "w1", created=1.0)
    wo.add_worker(rec, "w2", created=1.0, after=["w1"])
    _apply(rec, wo.plan(rec, _alive("w1", "w2")))
    # w1 deleted (merged and removed): done, and w2 goes.
    acts = wo.plan(rec, {"w1": {"exists": False}, **_alive("w2")})
    assert ("done", "w1", "gone") in acts and ("release", "w2", "") in acts
    # A held worker deleted before it started is forgotten.
    rec2 = _rec()
    wo.add_worker(rec2, "x", created=1.0, after=["y"])
    assert wo.plan(rec2, {"x": {"exists": False}}) == [
        ("forget", "x", "deleted before it started")
    ]


def test_steps_order_spawned_and_planned_workers():
    rec = _rec()
    wo.add_worker(rec, "w1", created=1.0)
    assert wo.set_policy(rec, steps=[["w1", "w2"], ["w3"]]) == []
    # w3 is not spawned yet: it picks the step up when it is.
    w3 = wo.add_worker(rec, "w3", created=1.0)
    assert set(w3["after"]) == {"w1", "w2"} and w3["why"]["w2"] == "step"
    view = wo.view(rec, _alive("w1", "w3"))
    assert [[w["title"] for w in s["workers"]] for s in view["steps"]] == [
        ["w1", "w2"],
        ["w3"],
    ]
    planned = view["steps"][0]["workers"][1]
    assert planned["planned"] and planned["word"] == "not started"
    # w2 not spawned → "missing" → w3 waits.
    _apply(rec, wo.plan(rec, _alive("w1", "w3", report={"w1": "done"})))
    assert rec["workers"]["w3"]["state"] == "held"
    assert (
        "w2 (not started yet)"
        in wo.view(rec, _alive("w1", "w3"))["steps"][1]["workers"][0]["detail"]
    )


def test_set_policy_refuses_cycles_and_bad_input_whole():
    rec = _rec()
    wo.add_worker(rec, "a", created=1.0)
    wo.add_worker(rec, "b", created=1.0, after=["a"])
    probs = wo.set_policy(rec, after={"a": ["b"]}, mode="serial")
    assert any("cycle" in p for p in probs)
    assert rec["mode"] == "parallel"  # nothing half-applied
    assert wo.set_policy(rec, mode="sideways")
    assert wo.set_policy(rec, max_parallel=99)
    assert wo.set_policy(rec, after={"nobody": []})
    assert wo.set_policy(rec, steps=[["a"], ["a"]])


def test_would_cycle_and_steps_of():
    assert wo.would_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]
    assert wo.would_cycle({"a": ["b"], "b": []}) == []
    rec = _rec(planned={"d": ["c"]})
    for t, after in (("a", []), ("b", ["a"]), ("c", ["a", "b"])):
        wo.add_worker(rec, t, created=1.0, after=after)
    assert wo.steps_of(rec) == {"a": 1, "b": 2, "c": 3, "d": 4}


def test_overlap_reason_and_fence_text():
    rec = _rec()
    wo.add_worker(rec, "w1", created=1.0)
    w = wo.add_worker(rec, "w2", created=1.0, overlaps={"w1": "src/api.py"})
    assert w["after"] == ["w1"] and w["why"]["w1"] == "overlap: src/api.py"
    text = wo.fence_text(
        {"only": ["src/**"], "keep_out": ["db/"], "reason": "x"}, "orch"
    )
    assert "set by orch" in text and "ONLY: src/**" in text and "OUT" in text
    assert wo.fence_text(None) == "" and wo.fence_text({"only": []}) == ""


def test_paths_overlap_is_the_split_rule():
    files = ["src/api.py", "src/db.py", "tests/test_api.py"]
    assert team_runs.paths_overlap(["src/**"], ["src/api.py"], files)
    assert team_runs.paths_overlap(["src/*.py"], ["src/api.py"], files)
    assert team_runs.paths_overlap(["**/*.py"], ["tests/**"], [])
    assert not team_runs.paths_overlap(["src/api.py"], ["src/db.py"], files)
    assert not team_runs.paths_overlap(["src/**"], [], files)


def test_store_keys_by_parent_creation_time():
    with wo.edit() as data:
        rec = wo.parent_rec(data, "orch", 10.0, make=True)
        wo.add_worker(rec, "w1", created=11.0)
    assert wo.parent_rec(wo.load(), "orch", 10.0)["workers"]
    # A namesake orchestrator (another creation time) starts clean.
    assert wo.parent_rec(wo.load(), "orch", 99.0) is None
    with wo.edit() as data:
        assert wo.parent_rec(data, "orch", 99.0, make=True)["workers"] == {}


# --------------------------------------------------------------------------- #
# The prompt queue's held flag
# --------------------------------------------------------------------------- #
def test_held_queue_stays_off_when_something_is_enqueued():
    prompt_queue.enqueue("w", "the task")
    prompt_queue.set_flags("w", enabled=False, held=True)
    prompt_queue.enqueue("w", "a message queued mid-hold")
    st = prompt_queue.get_state("w")
    assert st["enabled"] is False and st["held"] is True
    prompt_queue.set_flags("w", enabled=True, held=False)
    st = prompt_queue.get_state("w")
    assert st["enabled"] is True and "held" not in st


# --------------------------------------------------------------------------- #
# The driver, against a fake server
# --------------------------------------------------------------------------- #
def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path):
    os.makedirs(path)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    for rel in ("a/x.py", "b/y.py"):
        os.makedirs(os.path.join(path, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(path, rel), "w") as fh:
            fh.write("1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


class _WT:
    def __init__(self, sha):
        self.baseCommitSHA = sha

    def GetBaseCommitSHA(self):  # noqa: N802
        return self.baseCommitSHA


class _Inst:
    def __init__(self, title, wt, parent="", created=100.0, branch="", in_place=False):
        self.Title = title
        self.Parent = parent
        self.Branch = branch
        self.InPlace = in_place
        self.created = created
        self._wt = wt
        self.gw = _WT("")

    def GetWorktreePath(self):  # noqa: N802
        return self._wt

    def GetGitWorktree(self):  # noqa: N802
        return self.gw


@pytest.fixture
def fake(monkeypatch, tmp_path):
    instances: dict = {}
    told: list = []
    events: list = []
    srv = SimpleNamespace(
        ENGINE=SimpleNamespace(instances=instances),
        _prompt_queue=prompt_queue,
        _red_zones=rz,
        _team_runs=team_runs,
        _created_epoch=lambda inst: getattr(inst, "created", None),
        _deliver_to_agent=lambda inst, title, msg: (
            told.append((title, msg)),
            ("sent", None),
        )[1],
        _events=SimpleNamespace(
            BUS=SimpleNamespace(emit=lambda *a, **k: events.append((a, k)))
        ),
        _row_run=lambda t: runs.get(t),
        _agent_activity_cached=lambda inst, t: "idle",
    )
    srv.ENGINE.save = lambda: None
    runs: dict = {}
    monkeypatch.setattr(drv, "_server", lambda: srv)
    reports: dict = {}
    monkeypatch.setattr(thread, "last_report", lambda inst, t: reports.get(t))
    monkeypatch.setattr(
        mailbox, "last_result", lambda parent, t, since=None: reports.get(t)
    )
    monkeypatch.setattr(thread, "report_json", lambda m: m)
    srv.runs = runs
    srv.told, srv.events, srv.reports, srv.tmp = told, events, reports, tmp_path
    return srv


def _spawn(srv, title, parent, wt, payload, **kw):
    inst = _Inst(title, wt, parent=parent, **kw)
    srv.ENGINE.instances[title] = inst
    return drv.on_create(parent, title, inst, payload, None, "TASK " + title)


def test_unordered_spawn_is_untouched(fake):
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    order, err = _spawn(fake, "w1", "orch", repo, {})
    assert (order, err) == (None, None)
    assert wo.load()["parents"] == {}


def test_after_holds_then_releases_with_the_task(fake):
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    o1, _ = _spawn(fake, "w1", "orch", repo, {"after": []})
    assert o1 is None  # an empty after is no order
    _spawn(fake, "w1b", "orch", repo, {"fence": {"only": ["a/**"]}})
    o2, err = _spawn(fake, "w2", "orch", repo, {"after": ["w1b"]})
    assert err is None and o2["after"] == ["w1b"]
    drv.tick()
    # w1b: fenced + released; w2: held.
    assert prompt_queue.get_state("w1b")["enabled"] is True
    first = prompt_queue.list_queue("w1b")[0]["text"]
    assert first.startswith("MindFlock fence") and first.endswith("TASK w1b")
    assert prompt_queue.get_state("w2")["enabled"] is False
    row = drv.row_order("w2", "orch")
    assert row["state"] == "held" and row["detail"] == "after w1b"
    fake.reports["w1b"] = {"status": "done", "ts": 9e9}
    drv.tick()
    st = prompt_queue.get_state("w2")
    assert st["enabled"] is True and "held" not in st
    assert (
        "held this task until w1b finished" in prompt_queue.list_queue("w2")[0]["text"]
    )
    view = drv.order_view("orch")
    assert [[w["title"] for w in s["workers"]] for s in view["steps"]] == [
        ["w1b"],
        ["w2"],
    ]


def test_a_run_member_is_never_ordered_and_unordered_spawns_never_write(fake):
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    drv.set_order("orch", {"mode": "serial"})
    _spawn(fake, "w1", "orch", repo, {})
    o, err = _spawn(fake, "piece", "orch", repo, {"run_member": True})
    assert (o, err) == (None, None)
    assert "piece" not in wo.load()["parents"]["orch"]["workers"]
    fake.ENGINE.instances["solo"] = _Inst("solo", repo)
    before = os.stat(wo.store_path()).st_mtime_ns
    assert _spawn(fake, "x", "solo", repo, {}) == (None, None)
    assert os.stat(wo.store_path()).st_mtime_ns == before


def test_a_fenced_session_without_a_parent_is_held_until_fenced(fake):
    repo = _repo(str(fake.tmp / "r"))
    o, err = _spawn(fake, "lone", "", repo, {"fence": {"keep_out": ["b/"]}})
    assert err is None and o["held"] and o["after"] == []
    assert prompt_queue.get_state("lone")["enabled"] is False
    drv.tick()
    assert prompt_queue.get_state("lone")["enabled"] is True
    assert list(rz.session_fences(repo)) == ["mindflock_lone"]
    del fake.ENGINE.instances["lone"]
    drv.tick()
    assert wo.load()["parents"] == {}
    assert rz.session_fences(repo) == {}


def test_unknown_after_and_cycles_are_refused(fake):
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    _o, err = _spawn(fake, "w1", "orch", repo, {"after": ["ghost"]})
    assert "ghost" in err
    _o, err = drv.on_create("", "x", _Inst("x", repo), {"after": ["orch"]})
    assert "parent" in err


def test_overlapping_fences_serialize_unless_parallel(fake):
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    _spawn(fake, "w1", "orch", repo, {"fence": {"only": ["a/**"]}})
    o2, _ = _spawn(fake, "w2", "orch", repo, {"fence": {"only": ["a/x.py"]}})
    assert o2["after"] == ["w1"] and o2["why"]["w1"].startswith("overlap: a/x.py")
    o3, _ = _spawn(fake, "w3", "orch", repo, {"fence": {"only": ["b/**"]}})
    assert o3["after"] == []
    o4, _ = _spawn(
        fake, "w4", "orch", repo, {"fence": {"only": ["a/**"]}, "overlap": "parallel"}
    )
    assert o4["after"] == []


def test_set_order_serial_and_steps_via_driver(fake):
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    code, body = drv.set_order("orch", {"mode": "serial"})
    assert code == 200 and body["order"] is None  # nothing spawned yet
    _spawn(fake, "w1", "orch", repo, {})
    o2, _ = _spawn(fake, "w2", "orch", repo, {})
    assert o2["after"] == ["w1"] and o2["why"] == {"w1": "one at a time"}
    code, body = drv.set_order("orch", {"steps": "nope"})
    assert code == 400
    code, body = drv.set_order("ghost", {})
    assert code == 404


def test_fence_route_logic_per_session_only(fake):
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    # An in-place worker in the orchestrator's own folder.
    fake.ENGINE.instances["w1"] = _Inst("w1", repo, parent="orch", in_place=True)
    code, body = drv.set_fence(
        "w1", {"only": ["a/**"], "keep_out": ["b/"], "reason": "api only", "by": "orch"}
    )
    assert code == 200 and body["applied"] and body["shared_folder"] is True
    fences = rz.session_fences(repo)
    ((key, f),) = fences.items()
    assert key.endswith("w1") and f["by"] == "orch" and f["owner"] == "orch:orch"
    assert [z["pattern"] for z in f["red"]] == ["b/"]
    assert not f.get("companions") and f["no_commit"] is False
    # A running worker is told.
    assert fake.told and "ONLY: a/**" in fake.told[-1][1]
    # Shown as locked session zones on the Map.
    zones = rz.session_fence_zones(f)
    assert {(z["kind"], z["scope"], z["locked"], z["by"]) for z in zones} == {
        ("green", "session", True, "orch"),
        ("red", "session", True, "orch"),
    }
    # The guard carries it for that session only.
    with open(rz.guard_path(os.path.realpath(repo))) as fh:
        g = json.load(fh)
    entry = g["sessions"][key]
    assert entry["red_rules"][0]["pattern"] == "b/" and entry["red_files"] == ["b/y.py"]
    # Clear.
    code, body = drv.set_fence("w1", {"clear": True, "by": "orch"})
    assert code == 200 and rz.session_fences(repo) == {}
    assert drv.set_fence("w1", {"only": []})[0] == 400
    assert drv.set_fence("nobody", {"only": ["a"]})[0] == 404


def test_sweep_drops_fences_of_sessions_gone(fake):
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    fake.ENGINE.instances["w1"] = _Inst("w1", repo, parent="orch", in_place=True)
    drv.set_fence("w1", {"keep_out": ["b/"], "by": "orch"})
    rz.set_session_fence(repo, "mindflock_piece", ["a/**"], owner="run:r1")
    del fake.ENGINE.instances["w1"]
    drv.tick()
    # Only the orchestrator's fence goes; a split's is the run's to drop.
    assert list(rz.session_fences(repo)) == ["mindflock_piece"]


def test_release_catches_up_to_the_orchestrator_and_names_unmerged_work(fake):
    repo = _repo(str(fake.tmp / "r"))
    orch = fake.ENGINE.instances["orch"] = _Inst("orch", repo, branch="main")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    _git(repo, "branch", "w1-branch")
    _git(repo, "branch", "w3-branch")
    w1wt, w2wt, w3wt, w4wt = (str(fake.tmp / n) for n in ("w1", "w2", "w3", "w4"))
    _git(repo, "worktree", "add", "-q", w1wt, "w1-branch")
    _git(repo, "worktree", "add", "-q", "-b", "w2-branch", w2wt, head)
    _git(repo, "worktree", "add", "-q", w3wt, "w3-branch")
    _git(repo, "worktree", "add", "-q", "-b", "w4-branch", w4wt, head)
    with open(os.path.join(w1wt, "a", "x.py"), "w") as fh:
        fh.write("2\n")
    _git(w1wt, "commit", "-qam", "w1 work")
    _spawn(fake, "w1", "orch", w1wt, {}, branch="w1-branch")
    _spawn(fake, "w2", "orch", w2wt, {"after": ["w1"]}, branch="w2-branch")
    fake.ENGINE.instances["w2"].gw.baseCommitSHA = head
    drv.tick()
    # w1 done, NOT merged by the orchestrator: w2 is not moved; told instead.
    fake.reports["w1"] = {"status": "done", "ts": 9e9}
    drv.tick()
    task = prompt_queue.list_queue("w2")[0]["text"]
    assert "Not in your tree yet: w1 (branch w1-branch)" in task
    assert fake.ENGINE.instances["w2"].gw.baseCommitSHA == head
    # The orchestrator merges w3's work, then w4 (after w3) starts from it.
    with open(os.path.join(w3wt, "b", "y.py"), "w") as fh:
        fh.write("3\n")
    _git(w3wt, "commit", "-qam", "w3 work")
    _spawn(fake, "w3", "orch", w3wt, {}, branch="w3-branch")
    _spawn(fake, "w4", "orch", w4wt, {"after": ["w3"]}, branch="w4-branch")
    fake.ENGINE.instances["w4"].gw.baseCommitSHA = head
    drv.tick()
    _git(repo, "merge", "-q", "--ff-only", "w3-branch")
    fake.reports["w3"] = {"status": "done", "ts": 9e9}
    drv.tick()
    with open(os.path.join(w4wt, "b", "y.py")) as fh:
        assert fh.read() == "3\n"
    merged = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert fake.ENGINE.instances["w4"].gw.baseCommitSHA == merged
    assert (
        "fast-forwarded to orch's current HEAD"
        in prompt_queue.list_queue("w4")[0]["text"]
    )
    assert orch is fake.ENGINE.instances["orch"]


def test_a_held_worker_is_parked_before_it_is_registered(fake):
    """The review's race: a pass between registration and parking used to
    "release" an empty queue and leave the task stuck behind a held flag."""
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    seen = {}
    real_edit = wo.edit

    def spying_edit():
        seen["queue"] = prompt_queue.get_state("w2")
        return real_edit()

    _spawn(fake, "w1", "orch", repo, {})
    import backend.web.core.worker_order as wo_mod

    wo_mod.edit, saved = spying_edit, wo_mod.edit
    try:
        _spawn(fake, "w2", "orch", repo, {"after": ["w1"]})
    finally:
        wo_mod.edit = saved
    assert seen["queue"]["held"] is True and seen["queue"]["items"]
    # A queue switched on WITHOUT the held flag (a namesake's leftover) is not
    # read as "start now"; only the user's toggle on a parked queue is.
    drv.tick()
    assert drv.row_order("w2", "orch")["state"] == "held"
    prompt_queue.set_flags("w2", enabled=True)
    drv.tick()
    assert drv.row_order("w2", "orch")["state"] == "running"
    st = prompt_queue.get_state("w2")
    assert st["enabled"] and "held" not in st


def test_no_task_means_nothing_to_hold(fake):
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    _spawn(fake, "w1", "orch", repo, {})
    inst = _Inst("w2", repo, parent="orch")
    fake.ENGINE.instances["w2"] = inst
    order, err = drv.on_create("orch", "w2", inst, {"after": ["w1"]}, None, "")
    assert err is None and order["held"] is False
    assert drv.row_order("w2", "orch")["state"] == "running"


def test_review_refusals(fake):
    repo = _repo(str(fake.tmp / "r"))
    fake.ENGINE.instances["orch"] = _Inst("orch", repo)
    _o, err = _spawn(fake, "w1", "orch", repo, {"after": ["orch"]})
    assert "own orchestrator" in err
    fake.ENGINE.instances["piece"] = _Inst("piece", repo, parent="orch")
    fake.runs["piece"] = {"id": "r1"}
    code, body = drv.set_fence("piece", {"only": ["a/**"], "by": "orch"})
    assert code == 409 and "team run" in body["error"]


def test_enqueue_many_respects_held():
    prompt_queue.enqueue("w", "task")
    prompt_queue.set_flags("w", enabled=False, held=True)
    prompt_queue.enqueue_many("w", ["a", "b"])
    assert prompt_queue.get_state("w")["enabled"] is False


# --------------------------------------------------------------------------- #
# The guard hook: a session's keep-out, for that session only
# --------------------------------------------------------------------------- #
def _hook(payload, env, session):
    cmd = am.hook_command("working", tool_hook="pre")
    cp = subprocess.run(
        ["sh", "-c", cmd],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=dict(env, MINDFLOCK_SESSION_NAME=session),
    )
    out = cp.stdout.decode().strip()
    return json.loads(out) if out else None


def _write(repo, rel):
    return {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Write",
        "tool_input": {"file_path": os.path.join(repo, rel)},
        "tool_use_id": "tu",
    }


def _denied(out):
    return (
        bool(out)
        and (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
    )


def test_hook_enforces_a_session_keep_out_for_that_session_only(tmp_path):
    repo = _repo(str(tmp_path / "shared"))
    rz.set_session_fence(
        repo, "mindflock_w1", [], red=["b/"], name="kept out by orch", owner="orch:orch"
    )
    rz.sync_guard(os.path.realpath(repo), lroot=repo)
    env = {
        **os.environ,
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "markers"),
        "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "threads"),
    }
    env.pop("TMUX_PANE", None)
    out = _hook(_write(repo, "b/y.py"), env, "mindflock_w1")
    assert _denied(out)
    assert "kept out by orch" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert not _denied(_hook(_write(repo, "a/x.py"), env, "mindflock_w1"))
    # The orchestrator sharing the folder is not fenced.
    assert not _denied(_hook(_write(repo, "b/y.py"), env, "mindflock_orch"))


def test_hook_only_here_with_companions_in_its_own_worktree(tmp_path):
    repo = _repo(str(tmp_path / "own"))
    rz.set_session_fence(
        repo,
        "mindflock_w1",
        ["a/**"],
        owner="orch:orch",
        no_commit=False,
        companions=True,
    )
    rz.sync_guard(os.path.realpath(repo), lroot=repo)
    env = {
        **os.environ,
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "markers"),
        "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "threads"),
    }
    env.pop("TMUX_PANE", None)
    assert _denied(_hook(_write(repo, "b/y.py"), env, "mindflock_w1"))
    assert not _denied(_hook(_write(repo, "a/x.py"), env, "mindflock_w1"))
    # A lockfile is a companion: writable beside its paths.
    assert not _denied(_hook(_write(repo, "uv.lock"), env, "mindflock_w1"))


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
client = TestClient(server.app)


def test_routes_404_and_validation(monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    assert (
        client.post("/api/instances/nope/fence", json={"only": ["a"]}).status_code
        == 404
    )
    assert client.get("/api/instances/nope/order").status_code == 404
    assert client.post("/api/instances/nope/order", json={}).status_code == 404


def test_order_route_reads_and_sets(monkeypatch, tmp_path):
    inst = _Inst("orch", str(tmp_path))
    monkeypatch.setattr(server.ENGINE, "instances", {"orch": inst})
    monkeypatch.setattr(server, "_created_epoch", lambda i: 100.0)
    assert client.get("/api/instances/orch/order").json() == {"order": None}
    r = client.post(
        "/api/instances/orch/order",
        json={"steps": [["w1", "w2"], ["w3"]], "max_parallel": 2},
    )
    assert r.status_code == 200
    steps = r.json()["order"]["steps"]
    assert [[w["title"] for w in s["workers"]] for s in steps] == [["w1", "w2"], ["w3"]]
    assert r.json()["order"]["cap"] == 2
    bad = client.post("/api/instances/orch/order", json={"steps": [["a"], ["a"]]})
    assert bad.status_code == 400 and bad.json()["problems"]
