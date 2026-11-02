"""Flights round 18 backend (docs/flights.md "Round 18: free-floating chains"):
a stop's free ``pos`` (normaliser, PATCH, the layout route, surviving
tack-on), ``split_off`` (store + route, a Loop re-aimed, 409s) and the
``handoff`` kind "Start a chain" (config, judge, run against a fake server,
``wait``, ``triggered_by``) — on tmp stores, never the real engine."""

import asyncio
import math
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import handoff as H

NOW = 1_800_000_000.0
client = TestClient(server.app)

AGENT = {"kind": "agent", "config": {"prompt": "improve the sky"}}
CMD = {"kind": "command", "config": {"command": "make shots"}}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(D, "_JOBS", {})
    yield


def run(coro):
    return asyncio.run(coro)


def fly(*steps, **kw):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=NOW)
    kw.setdefault("session", "s")
    kw.setdefault("now", NOW)
    return F.take_off(plan, **kw)


def grounded(*steps, name=""):
    return F.ground(None, list(steps), name, now=NOW)


# --------------------------------------------------------------------------- #
# pos: the normaliser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,want",
    [
        ({"x": 10, "y": 20}, {"x": 10, "y": 20}),
        ({"x": 1.6, "y": -2.4}, {"x": 2, "y": -2}),
        ({"x": 3e9, "y": -3e9}, {"x": 100000, "y": -100000}),
        ({"x": 0, "y": 0, "z": 9}, {"x": 0, "y": 0}),
    ],
)
def test_pos_is_kept_as_clamped_ints(raw, want):
    assert F.normalize_step(dict(AGENT, pos=raw))["pos"] == want


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [1, 2],
        "10,20",
        {"x": 1},
        {"x": "1", "y": 2},
        {"x": True, "y": 2},
        {"x": math.nan, "y": 2},
        {"x": math.inf, "y": 2},
    ],
)
def test_a_bad_pos_is_dropped_never_an_error(raw):
    step = F.normalize_step(dict(AGENT, pos=raw), strict=True)
    assert "pos" not in step


def test_saved_plans_keep_pos():
    plan = F.save_plan(
        {"name": "Shape", "steps": [dict(AGENT, pos={"x": 150, "y": 0}), CMD]},
        now=NOW,
    )
    got = F.get_plan(plan["id"])
    assert got["steps"][0]["pos"] == {"x": 150, "y": 0}
    assert "pos" not in got["steps"][1]


def test_pos_survives_take_off_and_the_flight_dto():
    f = fly(dict(AGENT, pos={"x": 5, "y": 6}), CMD)
    assert F.get(f["id"])["steps"][0]["pos"] == {"x": 5, "y": 6}
    r = client.get("/api/flights/" + f["id"])
    assert r.json()["flight"]["steps"][0]["pos"] == {"x": 5, "y": 6}


def test_pos_survives_tack_on():
    f = fly(AGENT)
    F.op_add_step(f, dict(CMD, pos={"x": 150, "y": 0}), now=NOW)
    assert f["steps"][-1]["pos"] == {"x": 150, "y": 0}
    F.op_add_steps(
        f,
        [dict(CMD, id="s1", pos={"x": 300, "y": 10}), dict(CMD, id="s2")],
        now=NOW,
    )
    assert f["steps"][-2]["pos"] == {"x": 300, "y": 10}
    assert "pos" not in f["steps"][-1]
    plan = F.save_plan(
        {"name": "Mod", "steps": [dict(CMD, pos={"x": 1, "y": 2})]}, now=NOW
    )
    F.op_add_plan(f, plan, now=NOW)
    assert f["steps"][-1]["pos"] == {"x": 1, "y": 2}


def test_editing_a_step_keeps_its_pos():
    f = grounded(dict(CMD, pos={"x": 7, "y": 8}))
    F.op_edit_step(f, "s1", {"title": "Shots"}, now=NOW)
    assert f["steps"][0]["pos"] == {"x": 7, "y": 8}


# --------------------------------------------------------------------------- #
# pos: PATCH and the layout route
# --------------------------------------------------------------------------- #
def test_patch_pos_on_any_step_flown_current_or_finished():
    f = fly(AGENT, CMD, CMD)

    def flown(x):
        x["step_states"]["s1"]["state"] = "done"
        x["step_states"]["s2"]["state"] = "running"
        x["cursor"] = 1

    F.mutate(f["id"], flown)
    for sid in ("s1", "s2", "s3"):
        r = client.patch(
            "/api/flights/%s/steps/%s" % (f["id"], sid), json={"pos": {"x": 1, "y": 2}}
        )
        assert r.status_code == 200, r.text
        got = {s["id"]: s for s in r.json()["flight"]["steps"]}
        assert got[sid]["pos"] == {"x": 1, "y": 2}
    hist = F.get(f["id"])["history"]
    assert not any(h["event"] == "edited" for h in hist)  # layout writes no history
    # null clears
    r = client.patch("/api/flights/%s/steps/s1" % f["id"], json={"pos": None})
    assert "pos" not in r.json()["flight"]["steps"][0]
    # but a title edit of a flown step is still refused
    r = client.patch("/api/flights/%s/steps/s1" % f["id"], json={"title": "x"})
    assert r.status_code == 409
    # even a landed flight takes a position
    F.mutate(f["id"], lambda x: F.op_cancel(x, now=NOW))
    r = client.patch(
        "/api/flights/%s/steps/s3" % f["id"], json={"pos": {"x": 9, "y": 9}}
    )
    assert r.status_code == 200 and r.json()["flight"]["steps"][2]["pos"] == {
        "x": 9,
        "y": 9,
    }


def test_patch_pos_unknown_step_is_404():
    f = grounded(CMD)
    r = client.patch(
        "/api/flights/%s/steps/s9" % f["id"], json={"pos": {"x": 1, "y": 2}}
    )
    assert r.status_code == 404 and r.json()["error"] == "unknown_step"


def test_layout_route_sets_clears_and_ignores_unknown():
    f = grounded(dict(CMD, pos={"x": 1, "y": 1}), CMD, CMD)
    r = client.post(
        "/api/flights/%s/layout" % f["id"],
        json={
            "positions": {
                "s1": None,
                "s2": {"x": 150.4, "y": -20},
                "s3": {"x": "bad"},
                "s99": {"x": 1, "y": 1},
            }
        },
    )
    assert r.status_code == 200, r.text
    steps = r.json()["flight"]["steps"]
    assert "pos" not in steps[0]
    assert steps[1]["pos"] == {"x": 150, "y": -20}
    assert "pos" not in steps[2]
    assert [s["id"] for s in steps] == ["s1", "s2", "s3"]


def test_layout_route_refusals():
    f = grounded(CMD)
    r = client.post("/api/flights/%s/layout" % f["id"], json={"positions": [1]})
    assert r.status_code == 400 and r.json()["error"] == "bad_layout"
    r = client.post("/api/flights/f_nope/layout", json={"positions": {}})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# split_off
# --------------------------------------------------------------------------- #
def test_split_off_a_grounded_chain_with_a_loop():
    f = grounded(
        AGENT,
        dict(CMD, pos={"x": 150, "y": 0}),
        {"kind": "agent", "config": {"prompt": "edit"}, "pos": {"x": 300, "y": 0}},
        {"kind": "loop", "config": {"back_to": "s3", "times": 2}},
        name="Polish",
    )
    after, chain = F.split_off(f["id"], "s2", {"x": 40, "y": 400}, now=NOW)
    assert [s["id"] for s in after["steps"]] == ["s1"]
    assert set(after["step_states"]) == {"s1"}
    assert chain["state"] == F.GROUNDED and chain["session"] == ""
    assert chain["plan_id"] == F.ADHOC_PLAN_ID
    assert chain["plan_name"] == "Polish (from Run a command)"
    assert [s["id"] for s in chain["steps"]] == ["s1", "s2", "s3"]
    assert chain["steps"][0]["pos"] == {"x": 40, "y": 400}  # the drop
    assert chain["steps"][1]["pos"] == {"x": 300, "y": 0}  # kept
    assert chain["steps"][2]["config"]["back_to"] == "s2"  # re-aimed
    assert F.loop_target(chain, chain["steps"][2]) == 1
    assert all(ss["state"] == "pending" for ss in chain["step_states"].values())
    assert F.get(chain["id"]) == chain and F.get(f["id"]) == after
    assert any(
        h["event"] == "removed" and "split off" in h["note"] for h in after["history"]
    )


def test_split_off_without_pos_keeps_the_first_steps_own():
    f = grounded(AGENT, dict(CMD, pos={"x": 150, "y": 0}))
    _after, chain = F.split_off(f["id"], "s2", now=NOW)
    assert chain["steps"][0]["pos"] == {"x": 150, "y": 0}


def test_split_off_from_a_flight_in_the_air():
    f = fly(AGENT, CMD, CMD)
    after, chain = F.split_off(f["id"], "s2", now=NOW)
    assert [s["id"] for s in after["steps"]] == ["s1"]
    assert after["state"] == "running" and after["cursor"] == 0
    assert len(chain["steps"]) == 2


def test_split_off_refuses_a_started_step():
    f = fly(AGENT, CMD, CMD)
    # s1 is current: at/before the cursor
    with pytest.raises(F.FlightError) as err:
        F.split_off(f["id"], "s1", now=NOW)
    assert (err.value.error, err.value.status) == ("not_pending", 409)

    def started(x):
        x["step_states"]["s3"]["detail"] = {"performing": {"at": NOW}}

    F.mutate(f["id"], started)
    with pytest.raises(F.FlightError) as err:
        F.split_off(f["id"], "s2", now=NOW)
    assert err.value.error == "not_pending"
    assert len(F.get(f["id"])["steps"]) == 3  # nothing written
    assert len(F.list_flights()) == 1


def test_split_off_refuses_the_whole_chain_and_finished_flights():
    g = grounded(AGENT, CMD)
    with pytest.raises(F.FlightError) as err:
        F.split_off(g["id"], "s1", now=NOW)
    assert (err.value.error, err.value.status) == ("whole_chain", 409)
    f = fly(AGENT, CMD)
    F.mutate(f["id"], lambda x: F.op_cancel(x, now=NOW))
    with pytest.raises(F.FlightError) as err:
        F.split_off(f["id"], "s2", now=NOW)
    assert err.value.error == "finished"
    with pytest.raises(F.FlightError) as err:
        F.split_off(g["id"], "s9", now=NOW)
    assert err.value.error == "unknown_step"


def test_split_off_route():
    f = grounded(AGENT, CMD, CMD, name="Ship it")
    r = client.post(
        "/api/flights/%s/steps/s2/split_off" % f["id"], json={"pos": {"x": 5, "y": 6}}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["flight"]["steps"]) == 1 and len(body["chain"]["steps"]) == 2
    assert body["chain"]["steps"][0]["pos"] == {"x": 5, "y": 6}
    assert body["chain"]["plan_name"] == "Ship it (from Run a command)"
    # no body at all is fine
    r = client.post("/api/flights/%s/steps/s1/split_off" % body["chain"]["id"])
    assert r.status_code == 409 and r.json()["error"] == "whole_chain"
    r = client.post("/api/flights/f_nope/steps/s1/split_off")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# handoff: the kind, its config and title
# --------------------------------------------------------------------------- #
def test_handoff_dto_row():
    r = client.get("/api/flights/kinds")
    row = next(k for k in r.json()["kinds"] if k["kind"] == "handoff")
    assert (row["label"], row["family"], row["actor"]) == (
        "Start a chain",
        "flow",
        "robot",
    )
    fields = {f["name"]: f for f in row["fields"]}
    assert list(fields) == ["target", "session", "wait"]
    assert fields["target"]["dynamic"] == "chains"
    assert fields["target"]["type"] == "select"
    assert fields["session"]["dynamic"] == "sessions"
    assert fields["session"]["default"] == "new"
    assert fields["wait"] == {
        "name": "wait",
        "type": "bool",
        "label": "Wait for it to land",
        "default": False,
    }
    assert "handoff" in F.SESSION_KINDS and "handoff" not in F.AGENT_KINDS
    assert "flow" in F.FAMILIES


def test_handoff_config_and_titles():
    s = F.normalize_step({"kind": "handoff", "config": {"target": "flight:f_abc"}})
    assert s["config"] == {"target": "flight:f_abc", "session": "new", "wait": False}
    assert s["title"] == "Start a chain"
    s = F.normalize_step(
        {
            "kind": "handoff",
            "config": {"target": "plan:builtin:review@push", "session": "w", "wait": 1},
        }
    )
    assert s["config"]["target"] == "plan:builtin:review@push"
    assert s["title"] == "Start a chain on w and wait"
    s = F.normalize_step(
        {"kind": "handoff", "config": {"target": "flight:f_1", "session": "NEW"}}
    )
    assert s["config"]["session"] == "new"
    assert H.parse_target("flight:f_1") == ("flight", "f_1")
    assert H.parse_target("plan:p_1") == ("plan", "p_1")
    assert H.parse_target("session:x") == ("", "")


@pytest.mark.parametrize("config", [{}, {"target": ""}, {"target": "nope:1"}])
def test_handoff_strict_needs_a_target(config):
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "handoff", "config": config})
    assert err.value.field == "steps[0].config.target"


def test_handoff_on_the_ground_may_wait_for_its_target_but_not_take_off(
    monkeypatch,
):
    g = grounded({"kind": "handoff", "config": {}})
    assert g["steps"][0]["config"]["target"] == ""
    with pytest.raises(F.PlanError) as err:
        F.validate_for_takeoff(g)
    assert err.value.field == "steps[0].config.target"


# --------------------------------------------------------------------------- #
# handoff: the judge (pure)
# --------------------------------------------------------------------------- #
def _running_handoff(wait=True, child="f_c", session="s"):
    f = fly(
        {"kind": "handoff", "config": {"target": "flight:f_x", "wait": wait}},
        session=session,
    )
    ss = f["step_states"]["s1"]
    ss.update(state="running", since=NOW)
    ss["detail"] = {"child": child, "child_name": "Kid", "session": "w1"}
    return f


@pytest.mark.parametrize(
    "child,action,text",
    [
        ({"present": True, "state": "running", "name": "Kid"}, "wait", "Kid"),
        ({"present": True, "state": "paused", "name": "Kid"}, "wait", "Kid"),
        ({"present": True, "state": "done", "name": "Kid"}, "done", "Kid landed"),
        (
            {"present": True, "state": "halted", "reason": "tests red", "name": "Kid"},
            "fail",
            "Kid halted: tests red",
        ),
        (
            {"present": True, "state": "cancelled", "reason": "", "name": "Kid"},
            "fail",
            "Kid was cancelled",
        ),
        ({"present": False, "name": "Kid"}, "fail", "Kid is gone"),
    ],
)
def test_handoff_judge_with_wait(child, action, text):
    f = _running_handoff()
    out = F.next_action(f, {"session": {"present": True}, "handoff": child}, NOW)
    if action == "done":
        assert out["action"] == "done" and out["detail"]["receipt"] == text
    elif action == "fail":
        # on_fail halt resolves the fail into a halt with the reason
        assert out["action"] == "fail" and text in out["reason"]
    else:
        assert out["action"] == "wait" and text in out["note"]


def test_handoff_judge_without_obs_waits_and_without_wait_is_done():
    f = _running_handoff()
    assert F.next_action(f, {"session": {"present": True}}, NOW)["action"] == "wait"
    g = _running_handoff(wait=False, session="t")
    g["step_states"]["s1"]["detail"]["receipt"] = "started Kid on w1"
    out = F.next_action(g, {"session": {"present": True}}, NOW)
    assert out["action"] == "done" and out["detail"]["receipt"] == "started Kid on w1"


# --------------------------------------------------------------------------- #
# handoff: the run, against a fake server
# --------------------------------------------------------------------------- #
class FakeInst:
    Program = "claude"
    Branch = "feat"
    Status = SimpleNamespace(name="Running")

    def __init__(self, path="/repo"):
        self.Path = path

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
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: {
            "present": flight["session"] in ns.ENGINE.instances,
            "pending": False,
            "status": "running",
            "sleeping": False,
            "activity": "idle",
            "branch": "feat",
            "folder": "/repo",
        },
    )
    return ns


@pytest.fixture
def spawned(srv, monkeypatch):
    """``spawn_worker`` faked: records each call, makes the session live."""
    calls = []

    async def fake(flight, step, prompt, **kw):
        title = "w%d" % (len(calls) + 1)
        calls.append({"parent": flight["session"], "prompt": prompt, **kw})
        srv.ENGINE.instances[title] = FakeInst("/repo-" + title)
        return title

    monkeypatch.setattr(D, "spawn_worker", fake)
    return calls


def _step(fid, passes=1):
    for _ in range(passes):
        run(D.step_flight(fid))
    return F.get(fid)


def test_handoff_starts_a_grounded_chain_on_a_new_session(spawned):
    child = grounded(AGENT, CMD, name="Docs pass")
    f = fly({"kind": "handoff", "config": {"target": "flight:" + child["id"]}}, CMD)
    got = _step(f["id"])
    ss = got["step_states"]["s1"]
    assert ss["state"] == "done", ss
    assert ss["detail"]["child"] == child["id"]
    assert ss["detail"]["session"] == "w1"
    assert ss["detail"]["receipt"] == "started Docs pass on w1"
    assert spawned == [
        {
            "parent": "s",
            "prompt": "improve the sky",
            "report_back": False,
            "title_prefix": "Docs pass",
        }
    ]
    c = F.get(child["id"])
    assert c["state"] == "running" and c["session"] == "w1"
    assert c["repo_root"] == "/repo-w1" and c["branch"] == "feat"
    # seeded: the worker's initial prompt was the first agent stop
    assert c["step_states"]["s1"]["state"] == "running"
    assert c["step_states"]["s1"]["detail"] == {"seeded": True}
    assert c["triggered_by"] == {"flight": f["id"], "step": "s1", "session": "s"}
    r = client.get("/api/flights/" + child["id"])
    assert r.json()["flight"]["triggered_by"]["flight"] == f["id"]
    assert "triggered_by" not in F.get(f["id"])  # only a started flight has one


def test_handoff_onto_an_existing_session(srv, spawned):
    srv.ENGINE.instances["other"] = FakeInst("/elsewhere")
    child = grounded(CMD, name="Shots")
    f = fly(
        {
            "kind": "handoff",
            "config": {"target": "flight:" + child["id"], "session": "other"},
        }
    )
    got = _step(f["id"])
    assert got["step_states"]["s1"]["state"] == "done"
    assert spawned == []
    c = F.get(child["id"])
    assert c["session"] == "other" and c["state"] == "running"
    assert c["step_states"]["s1"]["state"] == "pending"  # the driver starts it


def test_handoff_a_saved_plan_is_a_fresh_copy_each_time(srv, spawned):
    plan = F.save_plan({"name": "Review", "steps": [AGENT, CMD]}, now=NOW)
    f = fly(
        {"kind": "handoff", "config": {"target": "plan:" + plan["id"]}},
        {"kind": "handoff", "config": {"target": "plan:" + plan["id"]}},
    )
    got = _step(f["id"], passes=3)
    kids = [got["step_states"][s]["detail"]["child"] for s in ("s1", "s2")]
    assert len(set(kids)) == 2
    for k, title in zip(kids, ("w1", "w2")):
        c = F.get(k)
        assert c["plan_id"] == plan["id"] and c["session"] == title
        assert [s["kind"] for s in c["steps"]] == ["agent", "command"]
    assert F.get_plan(plan["id"]) is not None  # never consumed


def test_handoff_refusals_fail_with_a_sentence(srv, spawned):
    def first_fail(cfg):
        f = fly({"kind": "handoff", "config": cfg})
        got = _step(f["id"])
        assert got["state"] == "halted", got["state"]
        return got["reason"]

    assert "gone" in first_fail({"target": "flight:f_gone"})
    assert "gone" in first_fail({"target": "plan:p_gone"})
    flying = fly(CMD, session="busy")
    srv.ENGINE.instances["busy"] = FakeInst()
    assert "already flying" in first_fail({"target": "flight:" + flying["id"]})
    g = grounded(CMD)
    assert "no session nobody" in first_fail(
        {"target": "flight:" + g["id"], "session": "nobody"}
    )
    assert "already on a flight" in first_fail(
        {"target": "flight:" + g["id"], "session": "busy"}
    )
    bad = grounded({"kind": "handoff", "config": {}}, name="Half")
    assert "Half" in first_fail({"target": "flight:" + bad["id"]})
    assert spawned == []
    assert F.get(g["id"])["state"] == F.GROUNDED


def test_handoff_a_failed_spawn_drops_the_plan_copy(srv, monkeypatch):
    async def refuse(*a, **kw):
        raise D._StepFailed("too many workers")

    monkeypatch.setattr(D, "spawn_worker", refuse)
    plan = F.save_plan({"name": "Review", "steps": [AGENT]}, now=NOW)
    f = fly({"kind": "handoff", "config": {"target": "plan:" + plan["id"]}})
    got = _step(f["id"])
    assert got["state"] == "halted" and "too many workers" in got["reason"]
    assert [x["id"] for x in F.list_flights()] == [f["id"]]


def test_handoff_with_wait_runs_until_the_child_lands(srv, spawned):
    child = grounded(CMD, name="Kid")
    f = fly(
        {
            "kind": "handoff",
            "config": {"target": "flight:" + child["id"], "wait": True},
        },
        CMD,
    )
    got = _step(f["id"])
    ss = got["step_states"]["s1"]
    assert ss["state"] == "running" and ss["detail"]["child"] == child["id"]
    got = _step(f["id"], passes=2)
    assert got["step_states"]["s1"]["state"] == "running"
    assert "waiting for Kid to land" in got["step_states"]["s1"]["note"]
    F.mutate(child["id"], lambda x: F._finish(x, "done", "", NOW))
    got = _step(f["id"])
    ss = got["step_states"]["s1"]
    assert ss["state"] == "done" and ss["detail"]["receipt"] == "Kid landed"
    assert got["cursor"] == 1


def test_handoff_with_wait_fails_when_the_child_is_cancelled(srv, spawned):
    child = grounded(CMD, name="Kid")
    f = fly(
        {"kind": "handoff", "config": {"target": "flight:" + child["id"], "wait": True}}
    )
    _step(f["id"])
    F.mutate(child["id"], lambda x: F.op_cancel(x, "no longer needed", now=NOW))
    got = _step(f["id"])
    assert got["state"] == "halted"
    assert "Kid was cancelled: no longer needed" in got["reason"]


def test_handoff_retry_adopts_its_own_started_child(srv, spawned):
    child = grounded(CMD, name="Kid")
    f = fly(
        {"kind": "handoff", "config": {"target": "flight:" + child["id"], "wait": True}}
    )
    _step(f["id"])
    # The step is wiped back to pending (a retry / a lost run): the child it
    # started is already flying with this stop's triggered_by — adopted.
    F.mutate(f["id"], lambda x: x["step_states"]["s1"].update(F._blank_step_state()))
    got = _step(f["id"])
    ss = got["step_states"]["s1"]
    assert ss["state"] == "running" and ss["detail"]["child"] == child["id"]
    assert len(spawned) == 1


def test_handoff_route_round_trip_via_tack_on(srv, spawned):
    child = grounded(CMD, name="Kid")
    f = fly(CMD)
    r = client.post(
        "/api/flights/%s/steps" % f["id"],
        json={
            "step": {
                "kind": "handoff",
                "config": {"target": "flight:" + child["id"], "session": "new"},
                "pos": {"x": 150, "y": 0},
            }
        },
    )
    assert r.status_code == 200, r.text
    step = r.json()["flight"]["steps"][-1]
    assert step["kind"] == "handoff" and step["pos"] == {"x": 150, "y": 0}
    r = client.post(
        "/api/flights/%s/steps" % f["id"],
        json={"step": {"kind": "handoff", "config": {}}},
    )
    assert r.status_code == 400 and r.json()["field"] == "step.config.target"
