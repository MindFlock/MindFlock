"""Flights round 11: grounded flights — stops with no session yet, and
attaching one (docs/flights.md "Grounded flights"). The store, the driver
(against a fake server) and the routes, all on a tmp store."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import flight_driver as D
from backend.web.core import flights as F

NOW = 1_800_000_000.0
client = TestClient(server.app)


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


def run(coro):
    return asyncio.run(coro)


def plan_of(*steps, name="Ticket to merge"):
    return F.save_plan({"name": name, "steps": list(steps)}, now=NOW)


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def test_ground_a_saved_stop_as_one_group():
    plan = plan_of({"kind": "agent", "config": {"prompt": "go"}}, {"kind": "ship"})
    f = F.ground(plan, now=NOW)
    assert (f["state"], f["session"], f["cursor"]) == ("grounded", "", 0)
    assert (f["plan_id"], f["plan_name"]) == (plan["id"], "Ticket to merge")
    assert [s["id"] for s in f["steps"]] == ["s1", "s2"]
    g = f["steps"][0]["group"]
    assert g["name"] == "Ticket to merge" and g["plan_id"] == plan["id"]
    assert f["steps"][1]["group"] == g
    assert all(ss["state"] == "pending" for ss in f["step_states"].values())
    assert F.get(f["id"])["state"] == "grounded"


def test_ground_steps_named_or_not():
    f = F.ground(None, [{"kind": "notify", "id": "x"}, {"kind": "wait"}], now=NOW)
    assert (f["plan_id"], f["plan_name"]) == ("adhoc", "Stops")
    assert [s["id"] for s in f["steps"]] == ["s1", "s2"]
    assert not any(s.get("group") for s in f["steps"])
    f = F.ground(None, [{"kind": "notify"}], name="Ping me", now=NOW)
    assert f["plan_name"] == "Ping me" and f["steps"][0]["group"]["name"] == "Ping me"
    with pytest.raises(F.FlightError) as err:
        F.ground(None, None, now=NOW)
    assert (err.value.status, err.value.error) == (400, "needs_steps")
    with pytest.raises(F.PlanError) as perr:
        F.ground(None, [{"kind": "moon"}], now=NOW)
    assert perr.value.field == "steps[0].kind"


def test_grounded_is_unfinished_never_pruned_never_bound():
    f = F.ground(None, [{"kind": "notify"}], now=NOW)
    assert [x["id"] for x in F.list_flights(include_finished=False)] == [f["id"]]
    assert F.prune(set(), now=NOW + 10 * F.GONE_GRACE_S) == []
    assert F.get(f["id"])["state"] == "grounded"
    assert F.for_session("") is None
    # Two grounded flights never clash over their (empty) session.
    F.ground(None, [{"kind": "notify"}], now=NOW)
    assert F.next_action(F.get(f["id"]), {}, NOW)["action"] == "wait"


def test_flight_ops_refuse_a_grounded_flight_but_steps_all_change():
    f = F.ground(None, [{"kind": "agent"}, {"kind": "notify"}, {"kind": "wait"}])
    for op, args in (
        (F.op_skip, ("s1",)),
        (F.op_retry, ("s1",)),
        (F.op_jump, ("s2",)),
        (F.op_resume, ()),
        (F.op_pause, ()),
        (F.op_done, ("s1",)),
    ):
        with pytest.raises(F.FlightError) as err:
            op(f, *args, now=NOW) if op is not F.op_done else op(f, *args)
        assert (err.value.status, err.value.error) == (409, "grounded"), op
    # Every step can change: the first one too, and at 0.
    F.op_edit_step(f, "s1", {"title": "Work"}, now=NOW)
    F.op_edit_step(f, "s1", {"halt": True}, now=NOW)
    F.op_add_step(f, {"kind": "notify", "title": "First"}, at=0, now=NOW)
    assert [s["id"] for s in f["steps"]] == ["s4", "s1", "s2", "s3"]
    F.op_move_step(f, "s3", 0, now=NOW)
    assert [s["id"] for s in f["steps"]] == ["s3", "s4", "s1", "s2"]
    F.op_remove_step(f, "s3", now=NOW)
    assert (f["state"], f["cursor"]) == ("grounded", 0)
    g = F.op_group(f, ["s4", "s1"], "Pair", now=NOW)
    assert f["steps"][0]["group"]["id"] == g["id"]
    F.op_cancel(f, now=NOW)
    assert f["state"] == "cancelled"


def test_op_attach_takes_off_or_halts_at_step_zero():
    f = F.ground(None, [{"kind": "agent"}, {"kind": "notify"}])
    F.op_attach(f, "s", "/repo", "feat", now=NOW)
    assert (f["state"], f["session"], f["repo_root"], f["branch"]) == (
        "running",
        "s",
        "/repo",
        "feat",
    )
    assert f["history"][-1]["event"] == "attached"
    assert f["step_states"]["s1"]["state"] == "pending"
    with pytest.raises(F.FlightError) as err:
        F.op_attach(f, "t")
    assert err.value.error == "not_grounded"
    h = F.ground(None, [{"kind": "notify", "halt": True}])
    F.op_attach(h, "s", now=NOW)
    assert (h["state"], h["reason"]) == ("paused", "halt point")
    k = F.ground(None, [{"kind": "agent", "config": {"prompt": "go"}}])
    F.op_attach(k, "s", seeded=True, now=NOW)
    assert k["step_states"]["s1"]["state"] == "running"
    assert k["step_states"]["s1"]["detail"] == {"seeded": True}


def test_attach_refuses_against_the_store():
    f = F.ground(None, [{"kind": "notify"}])
    plan = plan_of({"kind": "notify"})
    F.take_off(plan, session="busy", now=NOW)
    with pytest.raises(F.FlightError) as err:
        F.attach(f["id"], "busy")
    assert (err.value.status, err.value.error) == (409, "in_flight")
    with pytest.raises(F.FlightError) as err:
        F.attach("f_nope", "s")
    assert err.value.status == 404
    got = F.attach(f["id"], "s")
    assert got["state"] == "running" and F.get(f["id"])["session"] == "s"
    with pytest.raises(F.FlightError) as err:
        F.attach(f["id"], "t")
    assert err.value.error == "not_grounded"


# --------------------------------------------------------------------------- #
# The driver
# --------------------------------------------------------------------------- #
def test_the_driver_skips_grounded_flights(srv, monkeypatch):
    f = F.ground(None, [{"kind": "notify"}])
    stepped = []

    async def fake_step(fid):
        stepped.append(fid)

    monkeypatch.setattr(D, "step_flight", fake_step)
    run(D.run_pass())
    assert stepped == [] and F.get(f["id"])["state"] == "grounded"


def test_attach_an_existing_session_then_its_agent_step_is_typed(srv, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append((title, text)))
    f = run(D.ground({"steps": [{"kind": "agent", "config": {"prompt": "Do it"}}]}))
    woke = []
    monkeypatch.setattr(D, "wake", lambda: woke.append(1))
    got = run(D.attach(f["id"], {"title": "s"}))
    assert (got["state"], got["session"], got["repo_root"], got["branch"]) == (
        "running",
        "s",
        "/repo",
        "feat",
    )
    assert woke == [1]
    # The same path a start on an existing session takes: the driver's run.
    run(D.step_flight(f["id"]))
    assert sent == [("s", "Do it")]
    assert F.get(f["id"])["step_states"]["s1"]["detail"] == {"sent": True}


def test_attach_refusals(srv):
    f = run(D.ground({"steps": [{"kind": "notify"}]}))
    with pytest.raises(F.FlightError) as err:
        run(D.attach(f["id"], {"title": "ghost"}))
    assert (err.value.status, err.value.error) == (404, "unknown_session")
    with pytest.raises(F.FlightError) as err:
        run(D.attach("f_nope", {"title": "s"}))
    assert (err.value.status, err.value.error) == (404, "unknown_flight")
    with pytest.raises(F.FlightError) as err:
        run(D.attach(f["id"], {"title": ""}))
    assert (err.value.status, err.value.error) == (400, "no_repo")
    other = run(D.ground({"steps": [{"kind": "notify"}]}))
    run(D.attach(other["id"], {"title": "s"}))
    with pytest.raises(F.FlightError) as err:
        run(D.attach(f["id"], {"title": "s"}))
    assert (err.value.status, err.value.error) == (409, "in_flight")
    with pytest.raises(F.FlightError) as err:
        run(D.attach(other["id"], {"title": "s"}))
    assert (err.value.status, err.value.error) == (409, "not_grounded")


def test_attach_creates_the_session_with_the_first_prompt(srv, monkeypatch):
    import backend.web.core.session_create as sc

    created = []

    async def fake_create(payload, **kw):
        created.append(payload)
        title = "new-%d" % len(created)
        srv.ENGINE.instances[title] = FakeInst(payload["repo_path"])
        return 202, {"title": title}

    monkeypatch.setattr(sc, "create_result", fake_create)
    plan = F.save_plan(
        {
            "name": "Nightly",
            "session": {"repo_path": "/r", "model": "opus"},
            "steps": [{"kind": "agent", "config": {"prompt": "do {subject}"}}],
        },
        now=NOW,
    )
    f = run(D.ground({"plan_id": plan["id"]}))
    got = run(D.attach(f["id"], {"title": "", "program": "codex"}))
    assert created == [
        {
            "repo_path": "/r",
            "prompt": "do Nightly",
            "program": "codex",
            "profile_model": "opus",
        }
    ]
    assert got["session"] == "new-1" and got["repo_root"] == "/r"
    assert got["step_states"]["s1"]["state"] == "running"
    assert got["step_states"]["s1"]["detail"] == {"seeded": True}
    # No prompt anywhere: nothing typed, the step only waits for a turn.
    g = run(D.ground({"steps": [{"kind": "agent"}]}))
    got = run(D.attach(g["id"], {"title": "", "repo_path": "/q"}))
    assert created[1] == {"repo_path": "/q", "prompt": ""}
    assert got["session"] == "new-2"
    assert got["step_states"]["s1"]["detail"] == {"no_prompt": True}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {"sess": FakeInst()})


def test_routes_ground_list_attach_delete(engine):
    r = client.post(
        "/api/flights/start",
        json={"grounded": True, "steps": [{"kind": "notify"}], "name": "Pings"},
    )
    assert r.status_code == 202
    f = r.json()["flight"]
    assert (f["state"], f["session"], f["plan_name"]) == ("grounded", "", "Pings")
    assert [x["id"] for x in client.get("/api/flights").json()["flights"]] == [f["id"]]
    r = client.post("/api/flights/start", json={"grounded": True, "plan_id": "p_nope"})
    assert (r.status_code, r.json()["error"]) == (404, "unknown_plan")
    r = client.post("/api/flights/start", json={"grounded": True})
    assert (r.status_code, r.json()["error"]) == (400, "needs_steps")
    r = client.post("/api/flights/start", json={"grounded": True, "steps": "x"})
    assert (r.status_code, r.json()["error"]) == (400, "bad_step")
    r = client.post(
        "/api/flights/start", json={"grounded": True, "plan_id": "builtin:pr"}
    )
    g = r.json()["flight"]
    assert g["plan_id"] == "builtin:pr" and g["steps"][0]["group"]["plan_id"] == (
        "builtin:pr"
    )
    # Steps change on the ground; flight ops refuse.
    r = client.post(
        "/api/flights/%s/steps" % f["id"], json={"step": {"kind": "wait"}, "at": 0}
    )
    assert r.status_code == 200 and r.json()["flight"]["steps"][0]["kind"] == "wait"
    r = client.post("/api/flights/%s/resume" % f["id"])
    assert (r.status_code, r.json()["error"]) == (409, "grounded")
    # A grounded flight deletes at once.
    assert client.delete("/api/flights/" + g["id"]).status_code == 204
    # Attach.
    r = client.post("/api/flights/%s/attach" % f["id"], json={"title": "nope"})
    assert (r.status_code, r.json()["error"]) == (404, "unknown_session")
    r = client.post("/api/flights/%s/attach" % f["id"], json={"title": "sess"})
    assert r.status_code == 200
    got = r.json()["flight"]
    assert (got["state"], got["session"]) == ("running", "sess")
    assert got["history"][-1]["event"] == "attached"
    r = client.post("/api/flights/%s/attach" % f["id"], json={"title": "sess"})
    assert (r.status_code, r.json()["error"]) == (409, "not_grounded")
    assert client.delete("/api/flights/" + f["id"]).status_code == 409


def test_an_incomplete_block_may_sit_on_the_ground_but_not_take_off():
    """A Split with no prompts is fine on the board (the owner fills it in
    later); attaching refuses it by field (400 bad_step) instead."""
    import copy as _copy

    from backend.web.core import flights as F

    f = F.ground(None, [{"kind": "split", "title": "Fan out", "config": {}}], "")
    assert f["state"] == "grounded" and f["steps"][0]["kind"] == "split"
    # The same block on a flight in the air is refused as before.
    live = _copy.deepcopy(f)
    live["state"] = "running"
    with pytest.raises(F.PlanError) as err:
        F.op_add_step(live, {"kind": "split", "title": "x", "config": {}}, now=1.0)
    assert err.value.field.endswith(".prompts")
    with pytest.raises(F.PlanError) as err2:
        F.validate_for_takeoff(f)
    assert err2.value.field == "steps[0].config.prompts"
