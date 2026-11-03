"""Flights round 20 B backend (docs/flights.md "Round 20 … B. Parallel
stops"; notes in docs/flights-r20-parallel.md): ``parallel: {id}`` on a step,
consecutive steps sharing it = ONE group that flies side by side; the
parallel-safe kinds (``parallel_safe`` in the kinds dto, 400
``not_parallel_safe``); the cursor on a group's first member; the driver
working every open member each pass and going on only when all are through;
a failing member failing the group by its own ``on_fail``; ``POST /steps
{step, parallel_with}``, ``PATCH /steps/{sid} {parallel: null}`` /
``{parallel_with}``. On tmp stores, never the real engine."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import flight_driver as D
from backend.web.core import flights as F

NOW = 1_800_000_000.0
client = TestClient(server.app)

P = {"id": "p_abc123"}
NOTIFY = {"kind": "notify", "config": {"text": "one"}}
NOTIFY2 = {"kind": "notify", "config": {"text": "two"}}
WAIT = {"kind": "wait", "config": {"minutes": 5}}
CMD = {"kind": "command", "config": {"command": "make shots"}}
AGENT = {"kind": "agent", "config": {"prompt": "improve the sky"}}
YOU = {"kind": "you", "config": {"text": "look"}}


def par(step, pid=P):
    return dict(step, parallel=dict(pid))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(D, "_JOBS", {})
    yield


class FakeInst:
    def __init__(self, path="/repo"):
        self.Path = path
        self.Branch = "feat"
        self.Program = "claude"
        self.Status = SimpleNamespace(name="Running")

    def GetWorktreePath(self):
        return self.Path

    def GetGitWorktree(self):
        return SimpleNamespace(GetRepoPath=lambda: self.Path)


@pytest.fixture
def srv(monkeypatch):
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={"s": FakeInst()}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
        _publish_autopilot=lambda title: None,
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    monkeypatch.setattr(D, "_repo_root", lambda title: "/repo")

    def fake_obs(flight, now, agent):
        return {
            "present": flight["session"] == "s",
            "pending": False,
            "status": "running",
            "sleeping": False,
            "activity": "idle",
            "branch": "feat",
            "folder": "/repo",
            "pr_url": "",
        }

    monkeypatch.setattr(D, "_session_obs", fake_obs)
    return ns


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {"s": FakeInst()})
    monkeypatch.setattr(D, "_fasttrack_to_absorb", lambda title: None)
    monkeypatch.setattr(D, "_repo_root", lambda title: "/repo")
    monkeypatch.setattr(D, "wake", lambda: None)


def run(coro):
    return asyncio.run(coro)


def fly(*steps, now=NOW, **kw):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=now)
    kw.setdefault("session", "s")
    return F.take_off(plan, now=now, **kw)


def states(f):
    return [f["step_states"][s["id"]]["state"] for s in f["steps"]]


def pids(f):
    return [F.parallel_id(s) for s in f["steps"]]


# --------------------------------------------------------------------------- #
# Parallel-safe kinds and the dto
# --------------------------------------------------------------------------- #
SAFE = (
    "notify",
    "wait",
    "command",
    "checks",
    "ticket",
    "you",
    "test",
    "call",
    "listen",
    "handoff",
    "screenshot",
    "preview",
    "budget",
    "checkpoint",
    "someone",
    "review",
)
UNSAFE = (
    "agent",
    "ship",
    "reviews",
    "verify",
    "split",
    "join",
    "land",
    "sync",
    "loop",
    "judge",
)


def test_parallel_safe_per_kind():
    for kind in SAFE:
        assert F.parallel_safe(kind), kind
    for kind in UNSAFE:
        assert not F.parallel_safe(kind), kind
    assert not F.parallel_safe("nope")
    # Every registered kind is decided (a new kind must be listed above).
    assert set(F.STEP_KINDS) == set(SAFE) | set(UNSAFE)


def test_kinds_dto_carries_parallel_safe():
    dto = client.get("/api/flights/kinds").json()
    by = {k["kind"]: k["parallel_safe"] for k in dto["kinds"]}
    assert by["notify"] is True and by["agent"] is False and by["judge"] is False


def test_a_plug_in_that_types_to_the_agent_is_refused(monkeypatch):
    plug = SimpleNamespace(types_to_agent=True, spec={})
    real = F._kinds.get
    monkeypatch.setattr(F._kinds, "get", lambda k: plug if k == "notify" else real(k))
    assert not F.parallel_safe("notify")
    plug2 = SimpleNamespace(types_to_agent=False, spec={"parallel_safe": False})
    monkeypatch.setattr(F._kinds, "get", lambda k: plug2 if k == "notify" else real(k))
    assert not F.parallel_safe("notify")


# --------------------------------------------------------------------------- #
# The normaliser
# --------------------------------------------------------------------------- #
def test_normaliser_keeps_parallel_and_groups_are_runs():
    steps = F.normalize_steps([par(NOTIFY), par(NOTIFY2), WAIT])
    assert [F.parallel_id(s) for s in steps] == ["p_abc123", "p_abc123", ""]
    assert F.group_span(steps, 0) == (0, 2) == F.group_span(steps, 1)
    assert F.group_span(steps, 2) == (2, 3)


def test_a_group_of_one_is_no_group():
    steps = F.normalize_steps([par(NOTIFY), WAIT, par(NOTIFY2, {"id": "p_9"})])
    assert all("parallel" not in s for s in steps)


def test_a_second_run_reusing_an_id_gets_its_own_stably():
    raw = [par(NOTIFY), par(NOTIFY2), WAIT, par(NOTIFY), par(NOTIFY2)]
    a = F.normalize_steps(raw)
    b = F.normalize_steps(raw)
    assert F.parallel_id(a[0]) == "p_abc123"
    assert F.parallel_id(a[3]) == F.parallel_id(a[4]) != "p_abc123"
    assert F.parallel_id(a[3]) == F.parallel_id(b[3])  # derived, not random


def test_bad_parallel_ids_are_dropped():
    for bad in ({"id": "x"}, {"id": ""}, "p_1", {"id": "p_" + "a" * 40}, None):
        s = F.normalize_step(dict(NOTIFY, parallel=bad))
        assert "parallel" not in s


def test_an_unsafe_kind_is_refused_strictly_and_dropped_leniently():
    with pytest.raises(F.PlanError) as err:
        F.normalize_steps([par(AGENT), par(NOTIFY)])
    assert err.value.code == "not_parallel_safe"
    assert err.value.field == "steps[0].parallel"
    assert "use a Split for agents" in err.value.message
    steps = F.normalize_steps([par(AGENT), par(NOTIFY)], strict=False)
    assert all("parallel" not in s for s in steps)


def test_saved_plans_keep_parallel():
    plan = F.save_plan({"name": "P", "steps": [CMD, par(NOTIFY), par(NOTIFY2)]})
    again = F.get_plan(plan["id"])
    assert [F.parallel_id(s) for s in again["steps"]] == ["", "p_abc123", "p_abc123"]


def test_plan_route_answers_not_parallel_safe(engine):
    r = client.post(
        "/api/flights/start",
        json={"grounded": True, "steps": [par(AGENT), par(NOTIFY)]},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "not_parallel_safe"


# --------------------------------------------------------------------------- #
# Take off, ground, attach, split off
# --------------------------------------------------------------------------- #
def test_take_off_keeps_groups_and_the_cursor_stands_on_the_first_member():
    f = fly(CMD, par(NOTIFY), par(WAIT))
    assert pids(f) == ["", "p_abc123", "p_abc123"]
    g = F._normalize_flight(f["id"], dict(f, cursor=2))
    assert g["cursor"] == 1


def test_ground_attach_and_split_off_keep_parallel():
    g = F.ground(raw_steps=[CMD, par(NOTIFY), par(NOTIFY2), WAIT], now=NOW)
    assert pids(g) == ["", "p_abc123", "p_abc123", ""]
    g = F.attach(g["id"], "s", now=NOW)
    assert pids(g) == ["", "p_abc123", "p_abc123", ""]
    g2 = F.ground(raw_steps=[CMD, par(NOTIFY), par(NOTIFY2)], now=NOW)
    f, chain = F.split_off(g2["id"], "s2", now=NOW)
    assert pids(chain) == ["p_abc123", "p_abc123"]
    assert pids(f) == [""]
    # Splitting off INSIDE a group leaves a member on each side: no group.
    g3 = F.ground(raw_steps=[CMD, par(NOTIFY), par(NOTIFY2)], now=NOW)
    f, chain = F.split_off(g3["id"], "s3", now=NOW)
    assert pids(f) == ["", ""] and pids(chain) == [""]


def test_take_off_on_a_prompt_gates_every_member_of_a_first_group():
    plan = F.save_plan({"name": "P", "steps": [par(NOTIFY), par(WAIT)]})
    f = F.take_off(plan, session="s", start_on="prompt", now=NOW)
    F.op_take_off(f, "prompt", NOW)
    assert all(f["step_states"][s]["detail"].get("after_turn") for s in ("s1", "s2"))


# --------------------------------------------------------------------------- #
# The pure planner at a group
# --------------------------------------------------------------------------- #
def test_apply_at_keeps_the_cursor_until_every_member_is_through():
    f = fly(par(WAIT), par(NOTIFY), CMD)
    assert F.open_members(f) == [0, 1]
    F.apply_at(f, 1, {"action": "run", "note": ""}, NOW)
    assert f["cursor"] == 0 and states(f)[:2] == ["pending", "running"]
    F.apply_at(f, 1, {"action": "done", "note": "sent"}, NOW)
    assert f["cursor"] == 0 and F.open_members(f) == [0]
    F.apply_at(f, 0, {"action": "run", "note": ""}, NOW)
    F.apply_at(f, 0, {"action": "done", "note": ""}, NOW)
    assert f["cursor"] == 2 and f["state"] == "running"


def test_a_member_view_judges_its_own_step():
    f = fly(par(WAIT), par(NOTIFY))
    v = F.member_view(f, 1)
    assert F._current(v)["kind"] == "notify" and f["cursor"] == 0
    assert F.member_view(f, 0) is f


def test_a_failing_member_halts_the_group_by_its_on_fail():
    f = fly(par(WAIT), par(CMD), NOTIFY)
    F.apply_at(f, 0, {"action": "run", "note": ""}, NOW)
    F.apply_at(f, 1, {"action": "run", "note": ""}, NOW)
    F.apply_at(f, 1, {"action": "fail", "then": "halt", "reason": "exited 3"}, NOW)
    assert f["state"] == "halted" and f["reason"] == "exited 3"
    assert f["cursor"] == 0
    # Resume runs the failed member again; the other one keeps its state.
    F.op_resume(f, NOW + 1)
    assert f["state"] == "running" and states(f)[:2] == ["running", "pending"]


def test_a_skip_on_fail_member_moves_on_once_the_others_are_done():
    f = fly(par(WAIT), dict(par(CMD), on_fail="skip"), NOTIFY)
    F.apply_at(f, 1, {"action": "run", "note": ""}, NOW)
    F.apply_at(f, 1, {"action": "fail", "then": "skip", "reason": "nope"}, NOW)
    assert f["state"] == "running" and f["cursor"] == 0
    assert F.open_members(f) == [0]
    F.apply_at(f, 0, {"action": "run", "note": ""}, NOW)
    F.apply_at(f, 0, {"action": "done", "note": ""}, NOW)
    assert f["cursor"] == 2


def test_a_last_member_skip_on_fail_never_leaves_the_group_early():
    f = fly(par(WAIT), dict(par(CMD), on_fail="skip"), NOTIFY)
    F.apply_at(f, 0, {"action": "run", "note": ""}, NOW)
    F.apply_at(f, 1, {"action": "fail", "then": "skip", "reason": "nope"}, NOW)
    assert f["cursor"] == 0 and states(f) == ["running", "failed", "pending"]


def test_a_member_waiting_on_you_keeps_the_flight_waiting():
    f = fly(par(YOU), par(WAIT), NOTIFY)
    F.apply_at(f, 0, {"action": "you", "note": "look"}, NOW)
    assert f["state"] == "waiting_you"
    F.apply_at(f, 1, {"action": "run", "note": ""}, NOW)
    assert f["state"] == "waiting_you"
    F.apply_at(f, 1, {"action": "done", "note": ""}, NOW)
    assert f["state"] == "waiting_you" and f["cursor"] == 0
    assert F.you_text(f) == ("look", [])
    F.op_done(f, "s1", now=NOW)
    assert f["cursor"] == 2 and f["state"] == "running"


def test_done_on_a_second_member_waiting_on_you():
    f = fly(par(WAIT), par(YOU))
    F.apply_at(f, 1, {"action": "you", "note": "look"}, NOW)
    assert F.waiting_step(f)["id"] == "s2"
    F.op_done(f, "s2", now=NOW)
    assert f["cursor"] == 0 and f["state"] == "running"


def test_skip_a_member_and_jump_onto_one():
    f = fly(CMD, par(WAIT), par(NOTIFY), CMD)
    F.op_jump(f, "s3", NOW)
    assert f["cursor"] == 1  # the group's first member
    F.op_skip(f, "s2", NOW)
    assert f["cursor"] == 1 and F.open_members(f) == [2]
    F.op_skip(f, "s3", NOW)
    assert f["cursor"] == 3


def test_next_action_goes_past_a_group_every_member_of_which_is_through():
    f = fly(par(WAIT), par(NOTIFY), CMD)
    for sid in ("s1", "s2"):
        f["step_states"][sid]["state"] = "skipped"
    out = F.next_action(f, {"now": NOW, "session": {"present": True}}, NOW)
    assert out["action"] == "advance" and out.get("keep")


def test_step_view_lists_the_members():
    f = fly(par(WAIT), par(NOTIFY), CMD)
    view = F.step_view(f)
    assert [m["id"] for m in view["parallel"]] == ["s1", "s2"]
    assert "parallel" not in F.step_view(fly(CMD, session="t"))
    key = D._view_key(f)
    F.apply_at(f, 1, {"action": "run", "note": ""}, NOW)
    assert D._view_key(f) != key


def test_removing_a_flying_member_leaves_the_others_flying():
    f = fly(par(WAIT), par(NOTIFY), CMD)
    F.apply_at(f, 0, {"action": "run", "note": ""}, NOW)
    F.op_remove_step(f, "s2", NOW)
    assert f["state"] == "running" and f["cursor"] == 0
    assert pids(f) == ["", ""]


# --------------------------------------------------------------------------- #
# The driver
# --------------------------------------------------------------------------- #
def test_driver_runs_every_member_in_one_pass(srv, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_run_notify", lambda f, s: sent.append(s["config"]["text"]))
    f = fly(CMD, par(NOTIFY), par(NOTIFY2), WAIT, attach=True, start_at="s2")
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert sorted(sent) == ["one", "two"]
    assert states(got)[1:3] == ["done", "done"]
    # It went on past the group in the same pass: the wait is running.
    assert got["cursor"] == 3 and states(got)[3] == "running"


def test_driver_holds_the_group_while_a_member_still_runs(srv, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_run_notify", lambda f, s: sent.append(s["config"]["text"]))
    f = fly(par(WAIT), par(NOTIFY), CMD)
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert sent == ["one"]
    assert states(got) == ["running", "done", "pending"]
    assert got["cursor"] == 0 and got["state"] == "running"
    until = got["step_states"]["s1"]["detail"]["until"]
    assert 290 < until - time.time() <= 300
    # The timer ends: the group is through, the command starts.
    monkeypatch.setattr(D, "_start_command", lambda f, s, folder: None)
    F.mutate(
        f["id"],
        lambda g: g["step_states"]["s1"]["detail"].update(until=time.time() - 1),
    )
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert states(got)[:2] == ["done", "done"] and got["cursor"] == 2
    assert states(got)[2] == "running"


def test_driver_a_failing_member_halts_the_flight(srv, monkeypatch):
    def boom(f, s):
        raise D._StepFailed("no phone")

    monkeypatch.setattr(D, "_run_notify", boom)
    f = fly(par(WAIT), par(NOTIFY), CMD)
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert got["state"] == "halted" and got["reason"] == "no phone"
    assert states(got)[1] == "failed"


def test_driver_lone_steps_fly_as_before(srv, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_run_notify", lambda f, s: sent.append(s["config"]["text"]))
    f = fly(NOTIFY, NOTIFY2)
    run(D.step_flight(f["id"]))
    assert sent == ["one", "two"] and F.get(f["id"])["state"] == "done"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _ground(*steps):
    return F.ground(raw_steps=list(steps), now=NOW)


def test_post_parallel_with_makes_a_group(engine):
    g = _ground(CMD, NOTIFY, WAIT)
    r = client.post(
        "/api/flights/%s/steps" % g["id"],
        json={"step": NOTIFY2, "parallel_with": "s2"},
    )
    assert r.status_code == 200, r.text
    got = r.json()["flight"]
    assert [s["kind"] for s in got["steps"]] == ["command", "notify", "notify", "wait"]
    p = pids(got)
    assert p[0] == "" and p[1] and p[1] == p[2] and p[3] == ""
    assert p[1].startswith("p_")
    # A third member goes after the last one; ``at`` places it within.
    r = client.post(
        "/api/flights/%s/steps" % g["id"],
        json={"step": WAIT, "parallel_with": "s2", "at": 1},
    )
    got = r.json()["flight"]
    assert [s["kind"] for s in got["steps"]][:4] == [
        "command",
        "wait",
        "notify",
        "notify",
    ]
    assert len(set(pids(got)[1:4])) == 1
    r = client.post(
        "/api/flights/%s/steps" % g["id"],
        json={"step": WAIT, "parallel_with": "s2", "at": 0},
    )
    assert (r.status_code, r.json()["error"]) == (400, "bad_at")


def test_post_parallel_with_refuses_unsafe_kinds(engine):
    g = _ground(AGENT, NOTIFY)
    r = client.post(
        "/api/flights/%s/steps" % g["id"],
        json={"step": AGENT, "parallel_with": "s2"},
    )
    assert (r.status_code, r.json()["error"]) == (400, "not_parallel_safe")
    assert "use a Split for agents" in r.json()["detail"]
    r = client.post(
        "/api/flights/%s/steps" % g["id"],
        json={"step": NOTIFY2, "parallel_with": "s1"},
    )
    assert (r.status_code, r.json()["error"]) == (400, "not_parallel_safe")
    r = client.post(
        "/api/flights/%s/steps" % g["id"],
        json={"step": NOTIFY2, "parallel_with": "s9"},
    )
    assert (r.status_code, r.json()["error"]) == (404, "unknown_step")
    assert pids(F.get(g["id"])) == ["", ""]


def test_parallel_with_a_flying_step_is_in_progress_but_its_group_grows(engine):
    f = fly(WAIT, NOTIFY)
    F.mutate(f["id"], lambda g: F.apply(g, {"action": "run", "note": ""}, NOW))
    r = client.post(
        "/api/flights/%s/steps" % f["id"],
        json={"step": NOTIFY2, "parallel_with": "s1"},
    )
    assert (r.status_code, r.json()["error"]) == (409, "in_progress")
    f = fly(par(WAIT), par(NOTIFY), CMD, session="t")
    r = client.post(
        "/api/flights/%s/steps" % f["id"],
        json={"step": NOTIFY2, "parallel_with": "s1"},
    )
    assert r.status_code == 200, r.text
    got = r.json()["flight"]
    assert len(set(pids(got)[:3])) == 1 and pids(got)[0] and got["cursor"] == 0


def test_patch_parallel_null_takes_a_step_out(engine):
    g = _ground(par(NOTIFY), par(NOTIFY2), par(WAIT), CMD)
    r = client.patch("/api/flights/%s/steps/s1" % g["id"], json={"parallel": None})
    assert r.status_code == 200, r.text
    got = r.json()["flight"]
    # It flies right after the group it left.
    assert [s["id"] for s in got["steps"]] == ["s2", "s3", "s1", "s4"]
    assert pids(got) == ["p_abc123", "p_abc123", "", ""]
    r = client.patch("/api/flights/%s/steps/s2" % g["id"], json={"parallel": None})
    got = r.json()["flight"]
    assert pids(got) == ["", "", "", ""]  # a group of one is none
    r = client.patch(
        "/api/flights/%s/steps/s2" % g["id"], json={"parallel": {"id": "p_1"}}
    )
    assert (r.status_code, r.json()["error"]) == (400, "bad_parallel")


def test_patch_parallel_on_a_flying_member_is_in_progress(engine):
    f = fly(par(WAIT), par(NOTIFY), CMD)
    r = client.patch("/api/flights/%s/steps/s2" % f["id"], json={"parallel": None})
    assert (r.status_code, r.json()["error"]) == (409, "in_progress")


def test_patch_parallel_with_joins_a_group(engine):
    g = _ground(CMD, NOTIFY, WAIT, NOTIFY2)
    r = client.patch("/api/flights/%s/steps/s4" % g["id"], json={"parallel_with": "s2"})
    assert r.status_code == 200, r.text
    got = r.json()["flight"]
    assert [s["id"] for s in got["steps"]] == ["s1", "s2", "s4", "s3"]
    p = pids(got)
    assert p[1] and p[1] == p[2] and p[0] == p[3] == ""
    r = client.patch("/api/flights/%s/steps/s1" % g["id"], json={"parallel_with": "s2"})
    got = r.json()["flight"]
    assert [s["id"] for s in got["steps"]] == ["s2", "s4", "s1", "s3"]
    assert len(set(pids(got)[:3])) == 1
    r = client.patch("/api/flights/%s/steps/s3" % g["id"], json={"parallel_with": "s3"})
    assert (r.status_code, r.json()["error"]) == (400, "bad_parallel")


def test_patch_parallel_with_refuses_an_agent(engine):
    g = _ground(AGENT, NOTIFY)
    r = client.patch("/api/flights/%s/steps/s1" % g["id"], json={"parallel_with": "s2"})
    assert (r.status_code, r.json()["error"]) == (400, "not_parallel_safe")


def test_a_step_dropped_inside_a_group_joins_it(engine):
    g = _ground(par(NOTIFY), par(NOTIFY2), CMD)
    r = client.post("/api/flights/%s/steps" % g["id"], json={"step": WAIT, "at": 1})
    got = r.json()["flight"]
    assert len(set(pids(got)[:3])) == 1 and pids(got)[0]
    r = client.post("/api/flights/%s/steps" % g["id"], json={"step": AGENT, "at": 1})
    assert (r.status_code, r.json()["error"]) == (400, "not_parallel_safe")
    # At the group's edge it is a step of its own.
    r = client.post("/api/flights/%s/steps" % g["id"], json={"step": CMD, "at": 0})
    got = r.json()["flight"]
    assert pids(got)[0] == ""


def test_moving_a_step_inside_a_group_joins_it():
    g = _ground(par(NOTIFY), par(NOTIFY2), CMD, AGENT)
    F.op_move_step(g, "s3", 1, NOW)
    assert [s["id"] for s in g["steps"]][:3] == ["s1", "s3", "s2"]
    assert len({F.parallel_id(s) for s in g["steps"][:3]}) == 1
    with pytest.raises(F.PlanError) as err:
        F.op_move_step(g, "s4", 1, NOW)
    assert err.value.code == "not_parallel_safe"
