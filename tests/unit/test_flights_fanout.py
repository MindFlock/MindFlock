"""Flights fan-out and join (docs/flights.md "Fan-out and join"): the agent
step that waits for its workers, the ``split`` and ``join`` stops, the row
dto's ``join`` / ``branch`` and the MCP rows that carry the flight — the pure
planner against plain dicts, the driver against a fake server namespace."""

import asyncio
import copy
import datetime as _dt
import time
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

from backend.mcp import gitlocal as _gitlocal
from backend.mcp import tools as mcp_tools
from backend.web.core import events as _events
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import hidden_sessions as _hidden
from backend.web.core import session_create as _session_create
from backend.web.core import thread as _thread
from tests.unit._mcp_fakes import FakeCtx, make_box
from tests.unit._mcp_fakes import row as mcp_row

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    for knob, value in (
        ("MINDFLOCK_MAX_CHILDREN", "8"),
        ("MINDFLOCK_MAX_SPAWN_DEPTH", "3"),
        ("MINDFLOCK_MAX_SPAWNED", "24"),
    ):
        monkeypatch.setenv(knob, value)
    yield


def step(kind, **config):
    return {"kind": kind, "config": config}


def fly(*steps, session="s", now=NOW):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=now)
    return F.take_off(plan, session=session, now=now)


def sess(**kw):
    base = {"present": True, "status": "running", "activity": "idle"}
    base.update(kw)
    return base


def kid(title, **kw):
    """A worker's observed facts: idle and settled long ago unless told."""
    base = {
        "title": title,
        "present": True,
        "status": "running",
        "activity": "idle",
        "activity_since": NOW - 1000,
        "created": NOW - 1000,
        "settle_s": 8.0,
        "report": None,
    }
    base.update(kw)
    return base


def rep(ts, status="done", summary="did it", rid="m1"):
    return {"id": rid, "status": status, "summary": summary, "ts": ts}


def go(f, obs, now):
    """One planner pass: decide, apply, return the action."""
    a = F.next_action(f, obs, now)
    F.apply(f, a, now)
    return a


def started_agent(join=True):
    """A flight whose agent step has sent its prompt at NOW (and the agent
    has worked since)."""
    f = fly(step("agent", prompt="fan out", join=join), step("notify"))
    a = F.next_action(f, {"session": sess()}, NOW)
    assert a["action"] == "run"
    F.apply(f, dict(a, detail={"sent": True}), NOW)
    return f


def agent_obs(children, **s):
    return {"session": sess(worked_at=NOW + 1, **s), "children": children}


# --------------------------------------------------------------------------- #
# Registry + normaliser
# --------------------------------------------------------------------------- #
def test_kinds_serve_split_join_and_the_agent_join_switch():
    kinds = {k["kind"]: k for k in F.kinds_dto()["kinds"]}
    assert kinds["split"]["label"] == "Split" and kinds["split"]["actor"] == "robot"
    assert "follow it with a Join" in kinds["split"]["summary"]
    assert [f["name"] for f in kinds["split"]["fields"]] == [
        "prompts",
        "count",
        "model",
        "fence_only",
        "report_back",
    ]
    assert kinds["join"]["label"] == "Join"
    assert kinds["join"]["summary"] == "Wait until every worker has finished."
    of = next(f for f in kinds["join"]["fields"] if f["name"] == "of")
    assert of["options"] == ["all", "spawned"] and of["default"] == "all"
    on_failed = next(f for f in kinds["join"]["fields"] if f["name"] == "on_failed")
    assert on_failed["options"] == ["halt", "continue"]
    join = next(f for f in kinds["agent"]["fields"] if f["name"] == "join")
    assert join == {
        "name": "join",
        "type": "bool",
        "label": "Wait for its workers",
        "default": True,
    }


def test_normalise_split_join_and_agent_join():
    s = F.normalize_step({"kind": "agent", "config": {"prompt": "x"}})
    assert s["config"]["join"] is True
    s = F.normalize_step({"kind": "agent", "config": {"join": False}})
    assert s["config"]["join"] is False
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "split", "config": {"prompts": "  "}})
    assert err.value.field == "steps[0].config.prompts"
    # Strict refuses a count out of range (it used to be clamped silently);
    # a stored plan (lenient) still clamps it.
    with pytest.raises(F.PlanError, match="at most %d" % F.MAX_SPLIT) as err:
        F.normalize_step(
            {"kind": "split", "config": {"prompts": ["a", "b"], "count": 999}}
        )
    assert err.value.field == "steps[0].config.count"
    s = F.normalize_step(
        {"kind": "split", "config": {"prompts": ["a", "b"], "count": 999}},
        strict=False,
    )
    assert s["config"] == {
        "prompts": "a\nb",
        "count": F.MAX_SPLIT,
        "model": "",
        "fence_only": "",
        "report_back": True,
    }
    assert s["title"] == "Split"
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "join", "config": {"of": "some"}})
    assert err.value.field == "steps[0].config.of"
    with pytest.raises(F.PlanError):
        F.normalize_step({"kind": "join", "config": {"on_failed": "shrug"}})
    lenient = F.normalize_step(
        {"kind": "join", "config": {"of": "x", "on_failed": "y"}}, strict=False
    )
    assert lenient["config"] == {"of": "all", "on_failed": "halt"}
    assert lenient["title"] == "Join"


def test_split_prompts_fill_index_and_n():
    f = fly(step("notify"))
    one = {"prompts": "Fix part {index} of {n} for {subject}", "count": 3}
    assert F.split_prompts(one, f) == [
        "Fix part 1 of 3 for P",
        "Fix part 2 of 3 for P",
        "Fix part 3 of 3 for P",
    ]
    many = {"prompts": "api {index}/{n}\n\n  ui {index}/{n}  \n", "count": 5}
    assert F.split_prompts(many, f) == ["api 1/2", "ui 2/2"]
    assert F.split_prompts({"prompts": ""}, f) == []


def test_builtin_ticket_flyway_keeps_one_agent_step_that_joins():
    plan = F.get_plan("builtin:ticket")
    agents = [s for s in plan["steps"] if s["kind"] == "agent"]
    assert len(agents) == 1 and agents[0]["config"]["join"] is True
    assert not any(s["kind"] in ("split", "join") for s in plan["steps"])


# --------------------------------------------------------------------------- #
# Worker states
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "facts, state",
    [
        ({"present": False}, "gone"),
        ({"status": "paused"}, "gone"),
        ({"activity": "working", "activity_since": NOW + 5}, "working"),
        ({"activity": "clarify"}, "asking"),
        ({"activity": "limit"}, "working"),
        ({"status": "loading"}, "working"),
        ({"held": True}, "working"),
        ({"queue_pending": True}, "working"),
        ({"background_waiting": True}, "working"),
        ({"messages_pending": True}, "working"),
        ({"activity_since": 0}, "working"),
        # Idle, but not for its settle window yet (8 s claude / 30 s others).
        ({"activity_since": NOW + 95}, "working"),
        ({"activity_since": NOW + 95, "settle_s": 3.0}, "done"),
        # Idle long enough — but a worker this young has not started yet.
        ({"created": NOW + 60}, "working"),
        ({"activity": "offline", "activity_since": NOW + 80}, "working"),
        ({"activity": "offline", "activity_since": NOW + 60}, "done"),
        ({"activity": "offline", "sleeping": True}, "done"),
        ({}, "done"),
        ({"report": rep(NOW + 50)}, "done"),
        ({"report": rep(NOW + 50, "failed")}, "failed"),
        ({"report": rep(NOW + 50, "blocked")}, "asking"),
        # An earlier step's report does not count: it is judged by activity.
        (
            {"report": rep(NOW - 5), "activity": "working", "activity_since": NOW},
            "working",
        ),
        # Handed more work after reporting: the report is superseded.
        (
            {
                "report": rep(NOW + 50),
                "activity": "working",
                "activity_since": NOW + 60,
            },
            "working",
        ),
    ],
)
def test_child_state(facts, state):
    got = F.child_state(kid("w", **facts), None, NOW, NOW + 100)
    assert got["state"] == state, got
    assert got["title"] == "w"


def test_a_blocked_worker_that_works_again_settles_like_any_other():
    blocked = rep(NOW + 10, "blocked", "which db?")
    c = kid("w", report=blocked)
    first = F.child_state(c, None, NOW, NOW + 20)
    assert first["state"] == "asking"
    assert first["report"] == {
        "id": "m1",
        "status": "blocked",
        "summary": "which db?",
        "ts": NOW + 10,
    }
    busy = F.child_state(
        kid("w", report=blocked, activity="working", activity_since=NOW + 30),
        first,
        NOW,
        NOW + 40,
    )
    assert busy["state"] == "working"
    idle = F.child_state(
        kid("w", report=blocked, activity_since=NOW + 50), busy, NOW, NOW + 70
    )
    assert idle["state"] == "done"


# --------------------------------------------------------------------------- #
# The agent step joins its workers
# --------------------------------------------------------------------------- #
def test_agent_step_waits_for_a_working_worker_then_its_report():
    f = started_agent()
    kids = [kid("s-w1", activity="working", activity_since=NOW + 5), kid("s-w2")]
    a = go(f, agent_obs(kids), NOW + 10)
    assert a["action"] == "wait" and a["note"] == "the agent just went idle"
    assert f["step_states"]["s1"]["detail"]["waiting"] == 1
    assert F.join_dto(f) == {"waiting": 1, "total": 2}
    a = go(f, agent_obs(kids), NOW + 50)
    assert a["action"] == "wait" and a["note"] == "waiting for 1 of 2 workers"
    d = f["step_states"]["s1"]["detail"]
    assert [(e["title"], e["state"]) for e in d["children"]] == [
        ("s-w1", "working"),
        ("s-w2", "done"),
    ]
    # The worker reports at +60: the agent gets one settle window to read it.
    kids[0] = kid("s-w1", report=rep(NOW + 60), activity_since=NOW + 61)
    a = go(f, agent_obs(kids), NOW + 70)
    assert a["action"] == "wait"
    assert a["note"] == "the agent is reading its workers' reports"
    assert F.join_dto(f) is None
    a = go(f, agent_obs(kids), NOW + 80)
    assert a["action"] == "wait" and a["note"] == "confirming the agent is done"
    a = go(f, agent_obs(kids), NOW + 101)
    assert a["action"] == "done"
    assert a["note"] == "the agent and its 2 workers are done"
    assert f["cursor"] == 1
    assert f["step_states"]["s1"]["detail"]["children"][0]["report"]["ts"] == NOW + 60


def test_agent_step_keeps_waiting_while_a_worker_asks():
    f = started_agent()
    go(f, agent_obs([]), NOW + 10)
    a = go(f, agent_obs([kid("s-w1", activity="clarify")]), NOW + 50)
    assert a["action"] == "wait" and a["note"] == "worker s-w1 is asking"
    kids = [
        kid("s-w1", activity="clarify"),
        kid("s-w2", report=rep(NOW + 9, "blocked")),
    ]
    a = go(f, agent_obs(kids), NOW + 60)
    assert a["note"] == "workers s-w1, s-w2 are asking"
    assert F.join_dto(f) == {"waiting": 2, "total": 2}


def test_workers_spin_up_and_down_during_the_step():
    f = started_agent()
    go(f, agent_obs([]), NOW + 10)
    # No workers: the step is the plain settle gate (no join detail written).
    g = copy.deepcopy(f)
    a = F.next_action(g, agent_obs([]), NOW + 50)
    assert a["action"] == "done" and a["note"] == "the agent's turn settled"
    assert "children" not in (a.get("detail") or {})
    # A worker spins up: it joins the set.
    a = go(f, agent_obs([kid("s-w1", activity="working")]), NOW + 50)
    assert a["note"] == "waiting for 1 of 1 worker"
    # It is deleted: it leaves the set, and the step lands.
    a = go(f, agent_obs([]), NOW + 60)
    assert a["action"] == "done"
    assert f["step_states"]["s1"]["detail"]["total"] == 0


def test_a_closed_or_deleted_worker_counts_as_finished():
    f = started_agent()
    go(f, agent_obs([]), NOW + 10)
    kids = [kid("s-w1", status="paused"), kid("s-w2", present=False)]
    a = go(f, agent_obs(kids), NOW + 50)
    assert a["action"] == "done"
    assert [e["state"] for e in f["step_states"]["s1"]["detail"]["children"]] == [
        "gone",
        "gone",
    ]


def test_a_failed_worker_is_finished_for_the_agent_step():
    f = started_agent()
    go(f, agent_obs([]), NOW + 10)
    kids = [kid("s-w1", report=rep(NOW + 5, "failed", "tests red"))]
    a = go(f, agent_obs(kids), NOW + 50)
    assert a["action"] == "done"


def test_a_report_still_on_its_way_holds_the_agent():
    f = started_agent()
    go(f, agent_obs([kid("s-w1", report=rep(NOW + 5))]), NOW + 10)
    a = go(
        f,
        agent_obs([kid("s-w1", report=rep(NOW + 5))], messages_pending=True),
        NOW + 50,
    )
    assert a["action"] == "wait"
    assert a["note"] == "a worker's report is on its way to the agent"
    assert "idle_since" not in f["step_states"]["s1"]["detail"]


def test_join_false_ignores_the_workers():
    f = started_agent(join=False)
    go(f, agent_obs([kid("s-w1", activity="working")]), NOW + 10)
    a = go(f, agent_obs([kid("s-w1", activity="working")]), NOW + 50)
    assert a["action"] == "done"
    assert "children" not in f["step_states"]["s1"]["detail"]


def test_unobserved_workers_fall_back_to_the_settle_gate():
    f = started_agent()
    go(f, {"session": sess(worked_at=NOW + 1)}, NOW + 10)
    a = F.next_action(f, {"session": sess(worked_at=NOW + 1)}, NOW + 50)
    assert a["action"] == "done"


# --------------------------------------------------------------------------- #
# The join step
# --------------------------------------------------------------------------- #
def split_then_join(session="s", **join):
    f = fly(
        step("split", prompts="a\nb"),
        step("join", **join),
        step("notify"),
        session=session,
    )
    F.apply(f, {"action": "run", "note": ""}, NOW)
    F.apply(
        f,
        {"action": "done", "note": "", "detail": {"children": ["s-w1", "s-w2"]}},
        NOW + 1,
    )
    assert f["cursor"] == 1
    a = F.next_action(f, {"session": sess()}, NOW + 2)
    assert a["action"] == "run"
    F.apply(f, a, NOW + 2)
    return f


def test_join_of_spawned_names_the_splits_workers():
    f = split_then_join(of="spawned")
    assert F.spawned_titles(f) == ["s-w1", "s-w2"]
    assert F.join_since(f) == NOW  # the split's start: its workers' reports count
    kids = [kid("s-w1", report=rep(NOW + 5)), kid("s-w2", activity="working")]
    a = go(f, {"session": sess(), "children": kids}, NOW + 20)
    assert a["action"] == "wait" and a["note"] == "waiting for 1 of 2 workers"
    assert F.row_dto("s") is not None
    kids[1] = kid("s-w2", present=False)
    a = go(f, {"session": sess(), "children": kids}, NOW + 30)
    assert a["action"] == "done" and a["note"] == "all 2 workers finished"
    assert f["cursor"] == 2


def test_join_halts_on_a_failed_worker_or_carries_on():
    f = split_then_join()
    kids = [kid("s-w1", report=rep(NOW + 5, "failed", "the build broke"))]
    a = F.next_action(f, {"session": sess(), "children": kids}, NOW + 20)
    assert a["action"] == "fail" and a["then"] == "halt"
    assert a["reason"] == "worker s-w1 failed: the build broke"
    F.apply(f, a, NOW + 20)
    assert f["state"] == "halted"
    assert f["step_states"]["s2"]["detail"]["children"][0]["state"] == "failed"
    g = split_then_join(session="t", on_failed="continue")
    a = F.next_action(g, {"session": sess(), "children": kids}, NOW + 20)
    assert a["action"] == "done"


def test_join_with_no_workers_or_unread_workers():
    f = split_then_join()
    a = F.next_action(f, {"session": sess()}, NOW + 20)
    assert a["action"] == "wait" and a["note"] == "looking for the workers"
    a = F.next_action(f, {"session": sess(), "children": []}, NOW + 20)
    assert a["action"] == "done" and a["note"] == "no workers to wait for"


# --------------------------------------------------------------------------- #
# The row dto: join and branch
# --------------------------------------------------------------------------- #
def test_row_dto_join_and_a_workers_branch():
    f = started_agent()
    go(f, agent_obs([kid("s-w1", activity="working")]), NOW + 10)
    F.mutate(f["id"], lambda x: x.update(step_states=f["step_states"]))
    dto = F.row_dto("s")
    assert dto["join"] == {"waiting": 1, "total": 1}
    assert dto["branch"] is None
    # A worker with no flight of its own: a branch-only dto (never live).
    w = F.row_dto("s-w1", "s", NOW + 5)
    assert w == {
        "id": "",
        "plan_name": "P",
        "cursor": 0,
        "total": 0,
        "state": "",
        "reason": "",
        "step": None,
        "join": None,
        "branch": {"parent": "s", "step": "s1"},
    }
    assert F.row_dto("s-w1") is None  # no parent given: nothing to branch from
    assert F.row_dto("loner", "nobody-flying") is None


def test_branch_step_by_split_then_by_time():
    f = fly(step("agent", prompt="x"), step("split", prompts="a"), step("notify"))
    f["step_states"]["s1"].update(state="done", since=NOW)
    f["step_states"]["s2"].update(
        state="running", since=NOW + 100, detail={"children": ["s-w9"]}
    )
    f["cursor"] = 1
    assert F.branch_step(f, "s-w9", NOW + 5) == "s2"  # the split names it
    assert F.branch_step(f, "s-w1", NOW + 50) == "s1"
    assert F.branch_step(f, "s-w2", NOW + 150) == "s2"
    assert F.branch_step(f, "s-w3", None) == "s2"  # unknown age: the current step
    assert F.branch_step(f, "s-w4", NOW - 50) == "s2"


def test_a_worker_with_its_own_flight_carries_its_branch():
    fly(step("agent", prompt="x"), session="s")
    own = fly(step("notify"), session="s-w1")
    dto = F.row_dto("s-w1", "s", NOW)
    assert dto["id"] == own["id"] and dto["state"] == "running"
    assert dto["branch"] == {"parent": "s", "step": "s1"}


# --------------------------------------------------------------------------- #
# The driver: split executor, observing workers
# --------------------------------------------------------------------------- #
def at(epoch):
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc)


class FakeInst:
    def __init__(self, parent="", created=None, path="/repo"):
        self.Path = path
        self.Branch = "feat"
        self.Program = "claude"
        self.Parent = parent
        self.Spawned = bool(parent)
        self.Provisioned = False
        self.WorkspaceStrategy = "worktree"
        self.CreatedAt = at(created if created is not None else time.time())
        self.Status = SimpleNamespace(name="Running")

    def GetWorktreePath(self):
        return self.Path

    def GetGitWorktree(self):
        return SimpleNamespace(GetRepoPath=lambda: self.Path)


@pytest.fixture
def srv(monkeypatch):
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={"s": FakeInst(created=NOW - 5000)}),
        _pending_rows=lambda: [],
        _agent_mcp_caps=lambda: {"enabled": True, "providers": ["claude"]},
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: {
            "present": flight["session"] in ns.ENGINE.instances,
            "pending": False,
            "status": "running",
            "activity": "idle",
            "branch": "feat",
            "folder": "/repo",
        },
    )
    monkeypatch.setattr(D, "_row", lambda title: {"branch": "feat", "folder": "/repo"})
    monkeypatch.setattr(_gitlocal, "head_sha", lambda folder: "head-of-s")
    monkeypatch.setattr(_gitlocal, "canonical_repo_root", lambda folder: "/repo")
    monkeypatch.setattr(_gitlocal, "branch_exists", lambda repo, b: False)
    return ns


class Created(list):
    """The create payloads, in order; ``refuse[n]`` answers the n-th;
    ``autos`` the ``auto=`` each create passed (the launch gate's kind)."""

    refuse: dict
    autos: list


@pytest.fixture
def created(srv, monkeypatch):
    got = Created()
    got.refuse = {}
    got.autos = []
    refuse = got.refuse

    async def fake(payload, *, peer_share="", person="", auto=""):
        got.append(dict(payload))
        got.autos.append(auto)
        if len(got) in refuse:
            return refuse[len(got)]
        srv.ENGINE.instances[payload["title"]] = FakeInst(parent=payload["parent"])
        return 202, {"title": payload["title"], "status": "loading"}

    monkeypatch.setattr(_session_create, "create_result", fake)
    return got


def drive(f):
    asyncio.run(D.step_flight(f["id"]))
    return F.get(f["id"])


def driver_fly(*steps, session="s"):
    plan = F.save_plan({"name": "P", "steps": list(steps)})
    return F.take_off(plan, session=session)


def test_split_spawns_one_worker_per_prompt_forked_from_the_session(srv, created):
    f = driver_fly(
        step(
            "split",
            prompts="Do the API part {index}/{n}\nDo the UI part {index}/{n}",
            model="sonnet",
            fence_only="src/api/, tests/",
        ),
        step("wait", minutes=5),
    )
    got = drive(f)
    assert [p["title"] for p in created] == ["s-w1", "s-w2"]
    # Each worker is an automatic start: it asks the launch gate (which, with
    # memory fine, lets it go at once — the fake stands in for that answer).
    assert created.autos == ["flight", "flight"]
    first = created[0]
    assert first["parent"] == "s" and first["spawned"] is True
    assert first["repo_path"] == "/repo"
    assert first["base_ref"] == "head-of-s" and first["base_branch"] == "feat"
    assert first["program"] == "claude" and first["profile_model"] == "sonnet"
    assert first["fence"] == {"only": ["src/api/", "tests/"], "keep_out": []}
    assert first["prompt"].startswith("Do the API part 1/2")
    assert first["prompt"].endswith(mcp_tools.report_footer("s-w1", "s", "claude"))
    assert created[1]["prompt"].startswith("Do the UI part 2/2")
    ss = got["step_states"]["s1"]
    assert ss["state"] == "done" and ss["detail"]["children"] == ["s-w1", "s-w2"]
    assert ss["note"] == "spawned 2 workers"
    assert got["cursor"] == 1  # on to the next stop in the same pass


def test_a_split_goes_through_the_real_launch_gate_when_memory_is_fine(
    srv, monkeypatch
):
    """Not the faked create_result: the split's creates ask the real launch
    gate (``auto="flight"``), which with memory fine (or unknown) and nothing
    queued lets each go at once — a split never sits waiting for nothing."""
    from backend.web.core import launch_gate as _gate

    made = []

    async def fake_create(payload, **kw):
        made.append((payload["title"], kw.get("gated")))
        srv.ENGINE.instances[payload["title"]] = FakeInst(parent=payload["parent"])
        return JSONResponse({"title": payload["title"]}, status_code=202)

    async def fake_preflight(payload):
        # The parent "s" lives in this test's fake server, not the real
        # engine create's checks read: the request checks are not under test.
        ctx = {"parent": payload["parent"], "spawned": True, "base_ref": ""}
        return None, dict(ctx, repo="")

    monkeypatch.setattr(_session_create, "create", fake_create)
    monkeypatch.setattr(_session_create, "preflight", fake_preflight)
    got = drive(driver_fly(step("split", prompts="a\nb")))
    assert made == [("s-w1", True), ("s-w2", True)]
    assert _gate.items() == []
    assert got["step_states"]["s1"]["state"] == "done"


def test_split_without_report_back_or_attach_has_no_footer(srv, created):
    f = driver_fly(step("split", prompts="go", count=2, report_back=False))
    drive(f)
    assert [p["prompt"] for p in created] == ["go", "go"]
    srv._agent_mcp_caps = lambda: {"enabled": False, "providers": []}
    created.clear()
    srv.ENGINE.instances = {"s": FakeInst(created=NOW - 5000)}
    g = driver_fly(step("split", prompts="go"), session="t")
    srv.ENGINE.instances["t"] = FakeInst(created=NOW - 5000)
    drive(g)
    assert created[0]["prompt"] == "go" and "fence" not in created[0]


def test_a_refused_worker_fails_the_split_keeping_the_ones_made(srv, created):
    created.refuse[2] = (409, {"error": "memory is low — try again later"})
    f = driver_fly(step("split", prompts="a\nb\nc"))
    got = drive(f)
    assert got["state"] == "halted"
    assert got["reason"] == "memory is low — try again later (after 1 of 3 workers)"
    assert got["step_states"]["s1"]["detail"]["children"] == ["s-w1"]


def test_the_child_limit_refuses_before_anything_is_made(srv, created, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "2")
    f = driver_fly(step("split", prompts="a", count=3))
    got = drive(f)
    assert created == []
    assert got["state"] == "halted"
    assert "3 more workers would give s 3 live children (limit 2" in got["reason"]


def test_a_rerun_split_adopts_the_workers_it_already_made(srv, created):
    f = driver_fly(step("split", prompts="a\nb"))
    srv.ENGINE.instances["s-w1"] = FakeInst(parent="s")
    got = drive(f)
    assert [p["title"] for p in created] == ["s-w2"]
    assert created[0]["prompt"].startswith("b")
    assert got["step_states"]["s1"]["detail"]["children"] == ["s-w1", "s-w2"]


def test_children_obs_reads_rows_reports_and_skips_hidden_helpers(srv, monkeypatch):
    srv.ENGINE.instances.update(
        {
            "s-w1": FakeInst(parent="s", created=NOW),
            "s-w2": FakeInst(parent="s"),
            "other": FakeInst(),
        }
    )
    rows = [
        {
            "title": "s-w1",
            "status": "running",
            "activity": "working",
            "activity_since": NOW + 3,
            "provider": "codex",
            "queue": {"pending": 2},
            "order": {"state": "held"},
            "background": [{"state": "waiting"}],
        },
        {"title": "s-w2", "hidden_by": "verify"},
    ]
    monkeypatch.setattr(_events, "sessions_snapshot", lambda: rows)
    monkeypatch.setattr(_hidden, "hidden_by", lambda t: "")
    monkeypatch.setattr(
        _thread, "last_report", lambda insts, t: rep(NOW + 9) if t == "s-w1" else None
    )
    monkeypatch.setattr(D, "_messages_pending", lambda t: t == "s-w1")
    f = driver_fly(step("agent", prompt="x"))
    got = D._children_obs(f, f["steps"][0])
    assert got == [
        {
            "title": "s-w1",
            "present": True,
            "status": "running",
            "activity": "working",
            "activity_since": NOW + 3,
            "sleeping": False,
            "held": True,
            "queue_pending": True,
            "background_waiting": True,
            "messages_pending": True,
            "created": NOW,
            "settle_s": F.CHILD_SETTLE_OTHER_S,
            "report": rep(NOW + 9),
        }
    ]
    # of: spawned — the split's titles, a deleted one as not present.
    g = driver_fly(step("split", prompts="a"), step("join", of="spawned"), session="s2")
    g = F.mutate(g["id"], lambda x: x.update(session="s"))
    g = F.mutate(
        g["id"],
        lambda x: (
            x["step_states"]["s1"].update(
                state="done", detail={"children": ["s-w1", "s-w9"]}
            ),
            x.update(cursor=1),
        ),
    )
    got = D._children_obs(g, g["steps"][1])
    assert [(c["title"], c["present"]) for c in got] == [
        ("s-w1", True),
        ("s-w9", False),
    ]


def test_observe_carries_workers_and_the_parents_pending_report(srv, monkeypatch):
    seen = []
    monkeypatch.setattr(
        D, "_children_obs", lambda flight, step: seen.append(step["id"]) or []
    )
    monkeypatch.setattr(D, "_messages_pending", lambda t: True)
    f = driver_fly(step("agent", prompt="x"))
    F.mutate(f["id"], lambda x: F.apply(x, {"action": "run", "note": ""}, NOW))
    obs = asyncio.run(D.observe(F.get(f["id"]), NOW + 1))
    assert obs["children"] == [] and obs["session"]["messages_pending"] is True
    g = driver_fly(step("agent", prompt="x", join=False), session="t")
    g = F.mutate(g["id"], lambda x: F.apply(x, {"action": "run", "note": ""}, NOW))
    assert "children" not in asyncio.run(D.observe(g, NOW + 1))
    assert seen == ["s1"]


def test_child_settle_gates_match_wait_for_session():
    assert F.CHILD_SETTLE_CLAUDE_S == mcp_tools.SETTLE_CLAUDE_S
    assert F.CHILD_SETTLE_OTHER_S == mcp_tools.SETTLE_OTHER_S
    assert F.CHILD_OFFLINE_DONE_S == mcp_tools.OFFLINE_DONE_S
    assert F.CHILD_QUIET_S == mcp_tools.DISPATCH_QUIET_S
    assert F.child_settle_s("claude") == 8.0 and F.child_settle_s("codex") == 30.0


# --------------------------------------------------------------------------- #
# MCP rows carry the flight
# --------------------------------------------------------------------------- #
def test_mcp_session_rows_carry_the_flight():
    flight = {
        "id": "f_1",
        "plan_name": "P",
        "cursor": 0,
        "total": 3,
        "state": "running",
        "reason": "",
        "step": {
            "id": "s1",
            "kind": "agent",
            "title": "Agent works",
            "state": "running",
            "actor": "robot",
        },
        "join": {"waiting": 2, "total": 2},
        "branch": None,
    }
    branch = dict(flight, id="", state="", step=None, join=None)
    branch["branch"] = {"parent": "orch", "step": "s1"}
    rows = [
        mcp_row("orch", flight=flight),
        mcp_row("w1", parent="orch", spawned=True, flight=branch),
        mcp_row("w2", parent="orch", spawned=True),
    ]
    box, _, clock = make_box(rows)
    ctx = FakeCtx(clock)
    assert box.whoami({}, ctx)["session"]["flight"] == flight
    listed = {
        r["title"]: r for r in box.list_sessions({"filter": "all"}, ctx)["sessions"]
    }
    assert listed["w1"]["flight"]["branch"] == {"parent": "orch", "step": "s1"}
    assert "flight" not in listed["w2"]
    assert box.get_session({"title": "orch"}, ctx)["session"]["flight"] == flight
