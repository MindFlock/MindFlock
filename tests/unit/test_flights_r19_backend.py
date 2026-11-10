"""Flights round 19 backend (docs/flights.md "Round 19: a chain starts when
you say so"): the ``ready`` state and ``start_on`` (create / attach, editable
ops, holding its session), ▶ Run and ``PATCH {start_on}``, taking off on the
next prompt (the ``/send`` route, the queue drain, the mailbox lane, a turn
typed into the terminal caught by the tick), the ``after_turn`` gate (an
empty Agent stop counts as that turn; a turn under way when it became ready
does not) — and every default left as it was. On tmp stores, never the real
engine."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.mcp import flights as mcp_flights
from backend.web import server
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import queue_driver as Q

NOW = 1_800_000_000.0
client = TestClient(server.app)

AGENT = {"kind": "agent", "config": {"prompt": "improve the sky"}}
EMPTY_AGENT = {"kind": "agent"}
CMD = {"kind": "command", "config": {"command": "make shots"}}
NOTIFY = {"kind": "notify"}


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
    """A fake server for the driver: one live session ``s``."""
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={"s": FakeInst()}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
        _publish_autopilot=lambda title: None,
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    monkeypatch.setattr(D, "_fasttrack_to_absorb", lambda title: None)
    monkeypatch.setattr(D, "_repo_root", lambda title: "/repo")
    return ns


@pytest.fixture
def engine(monkeypatch):
    """The real server's engine with one fake live session ``s`` (routes)."""
    monkeypatch.setattr(server.ENGINE, "instances", {"s": FakeInst()})
    monkeypatch.setattr(D, "_fasttrack_to_absorb", lambda title: None)
    monkeypatch.setattr(D, "_repo_root", lambda title: "/repo")
    monkeypatch.setattr(D, "wake", lambda: None)


def run(coro):
    return asyncio.run(coro)


def plan_of(*steps):
    return F.save_plan({"name": "P", "steps": list(steps)}, now=NOW)


def ready(*steps, start_on="prompt", session="s", now=NOW):
    return F.take_off(
        plan_of(*steps), session=session, attach=True, start_on=start_on, now=now
    )


def sess(activity="idle", **kw):
    out = {
        "present": True,
        "pending": False,
        "status": "running",
        "sleeping": False,
        "activity": activity,
        "queue_pending": False,
        "limited": False,
        "background_waiting": False,
        "worked_at": 0.0,
        "turn_ended_at": 0.0,
    }
    out.update(kw)
    return out


def step_once(f, s, now):
    """One planner decision applied, as the driver's effect-free path does."""
    action = F.next_action(f, {"now": now, "session": s}, now)
    F.apply(f, action, now)
    return action


# --------------------------------------------------------------------------- #
# The ready state and start_on
# --------------------------------------------------------------------------- #
def test_ready_is_a_holding_unfinished_state_the_driver_never_moves():
    assert "ready" in F.STATES
    assert "ready" in F.UNFINISHED and "ready" in F.HOLDING
    assert "ready" not in F.ACTIVE
    assert F.START_ON == ("prompt", "run", "now")
    assert "took_off" in F.HISTORY_EVENTS
    assert "ready" in mcp_flights.UNFINISHED


@pytest.mark.parametrize("start_on", ["prompt", "run"])
def test_take_off_ready_on_a_session_starts_nothing(start_on):
    # A leading agent stop is NOT skipped (attach would skip it when flying
    # at once): a ready chain sits at 0 with every stop pending.
    f = ready(AGENT, CMD, start_on=start_on)
    assert f["state"] == "ready" and f["cursor"] == 0
    assert f["start_on"] == start_on and f["ready_since"] == NOW
    assert {st["state"] for st in f["step_states"].values()} == {"pending"}
    assert all(not st["detail"] for st in f["step_states"].values())
    assert [h["event"] for h in f["history"]] == []
    dto = client.get("/api/flights/" + f["id"]).json()["flight"]
    assert (dto["state"], dto["start_on"], dto["ready_since"]) == (
        "ready",
        start_on,
        NOW,
    )


def test_now_is_the_default_and_flies_at_once():
    f = F.take_off(plan_of(AGENT, CMD), session="s", attach=True, now=NOW)
    # The Agent stop has a prompt: attached, it is sent (cursor on it).
    assert f["state"] == "running" and f["cursor"] == 0
    assert (f["start_on"], f["ready_since"]) == ("now", 0.0)
    old = F._normalize_flight("f_old", {"id": "f_old", "steps": [CMD]})
    assert (old["start_on"], old["ready_since"]) == ("now", 0.0)
    bad = F._normalize_flight("f_x", {"id": "f_x", "steps": [CMD], "start_on": "z"})
    assert bad["start_on"] == "now"


def test_normalize_start_on():
    assert F.normalize_start_on(None) == "now"
    assert F.normalize_start_on("") == "now"
    assert F.normalize_start_on("Prompt") == "prompt"
    with pytest.raises(F.FlightError) as err:
        F.normalize_start_on("later")
    assert (err.value.status, err.value.error) == (400, "bad_start_on")


def test_a_ready_flight_holds_its_session():
    ready(CMD)
    with pytest.raises(F.FlightError) as err:
        ready(NOTIFY)
    assert (err.value.status, err.value.error) == (409, "in_flight")
    with pytest.raises(F.FlightError) as err:
        F.take_off(plan_of(NOTIFY), session="s", attach=True, now=NOW)
    assert err.value.error == "in_flight"
    g = F.ground(None, [NOTIFY], now=NOW)
    with pytest.raises(F.FlightError) as err:
        F.attach(g["id"], "s")
    assert err.value.error == "in_flight"


def test_attach_a_grounded_chain_as_ready():
    g = F.ground(None, [AGENT, CMD], now=NOW)
    got = F.attach(
        g["id"], "s", "/repo", "feat", seeded=True, now=NOW + 5, start_on="prompt"
    )
    assert (got["state"], got["session"], got["cursor"]) == ("ready", "s", 0)
    assert got["start_on"] == "prompt" and got["ready_since"] == NOW + 5
    # seeded never applies to a ready chain: nothing is running.
    assert got["step_states"]["s1"] == F._blank_step_state()
    assert [h["event"] for h in got["history"]] == ["attached"]
    # Default: flies at once, as today.
    h = F.ground(None, [CMD], now=NOW)
    assert F.attach(h["id"], "t")["state"] == "running"


def test_every_stop_of_a_ready_flight_can_change():
    f = ready(AGENT, CMD, CMD)
    fid = f["id"]
    # tack on at the very front (a running flight refuses index 0)
    f = F.mutate(fid, lambda x: F.op_add_step(x, NOTIFY, 0, now=NOW))
    assert [s["kind"] for s in f["steps"]] == ["notify", "agent", "command", "command"]
    first = f["steps"][0]["id"]
    agent = f["steps"][1]["id"]
    # edit the first stop (the current one of a flying flight could not)
    f = F.mutate(fid, lambda x: F.op_edit_step(x, agent, {"title": "Sky"}, now=NOW))
    assert f["steps"][1]["title"] == "Sky"
    # move a stop to index 0
    f = F.mutate(fid, lambda x: F.op_move_step(x, agent, 0, now=NOW))
    assert f["steps"][0]["id"] == agent
    # drop the first stop: nothing pauses, the flight stays ready
    f = F.mutate(fid, lambda x: F.op_remove_step(x, agent, now=NOW))
    assert f["state"] == "ready" and f["cursor"] == 0 and f["reason"] == ""
    # group, layout, halt on the first stop
    f = F.mutate(
        fid, lambda x: F.op_group(x, [x["steps"][0]["id"], x["steps"][1]["id"]], "G")
    )
    assert f["steps"][0]["group"]["name"] == "G"
    f = F.mutate(fid, lambda x: F.op_layout(x, {first: {"x": 3, "y": 4}}))
    assert f["steps"][0]["pos"] == {"x": 3, "y": 4}
    f = F.mutate(fid, lambda x: F.op_edit_step(x, first, {"halt": True}, now=NOW))
    assert f["steps"][0]["halt"] is True
    # split off the tail into a grounded chain
    after, chain = F.split_off(fid, f["steps"][1]["id"])
    assert after["state"] == "ready" and chain["state"] == "grounded"
    assert F.get(fid)["state"] == "ready"


def test_flight_ops_refuse_a_ready_flight_but_cancel_and_rename_work():
    f = ready(CMD, NOTIFY)
    for op in (
        lambda x: F.op_skip(x, "s1"),
        lambda x: F.op_retry(x, "s1"),
        lambda x: F.op_jump(x, "s2"),
        lambda x: F.op_pause(x),
        lambda x: F.op_resume(x),
        lambda x: F.op_done(x, "s1"),
    ):
        with pytest.raises(F.FlightError) as err:
            F.mutate(f["id"], op)
        assert (err.value.status, err.value.error) == (409, "not_running")
    assert F.mutate(f["id"], lambda x: F.op_rename(x, "Shots"))["plan_name"] == "Shots"
    assert F.mutate(f["id"], lambda x: F.op_cancel(x))["state"] == "cancelled"


def test_a_ready_flight_can_be_deleted_at_once():
    f = ready(CMD)
    D.remove(f["id"])
    assert F.get(f["id"]) is None


def test_the_driver_never_steps_a_ready_flight(srv, monkeypatch):
    f = ready(CMD, start_on="run")
    stepped = []

    async def fake_step(fid):
        stepped.append(fid)

    monkeypatch.setattr(D, "step_flight", fake_step)
    monkeypatch.setattr(
        D, "_row", lambda title: {"activity": "working", "activity_since": NOW + 9}
    )
    run(D.run_pass())
    # start_on "run": even a fresh turn in the terminal does not take it off
    assert stepped == [] and F.get(f["id"])["state"] == "ready"


def test_event_detail_says_how_it_starts():
    assert F.event_data(ready(CMD))["detail"] == "s: ready · runs on your next prompt"
    assert F.event_data(ready(CMD, session="t", start_on="run"))["detail"] == (
        "t: ready · runs when you press Run"
    )


# --------------------------------------------------------------------------- #
# Driver start / attach with start_on
# --------------------------------------------------------------------------- #
def test_driver_start_on_a_session_ready(srv):
    f = run(D.start({"title": "s", "steps": [AGENT, CMD], "start_on": "prompt"}))
    assert (f["state"], f["cursor"], f["start_on"]) == ("ready", 0, "prompt")
    with pytest.raises(F.FlightError) as err:
        run(D.start({"title": "s", "steps": [CMD], "start_on": "soon"}))
    assert err.value.error == "bad_start_on"


def test_driver_start_ready_absorbs_a_fast_track_as_leading_steps(srv, monkeypatch):
    rec = {"state": "running", "depth": "push", "lane": "push", "source": "user"}
    monkeypatch.setattr(D, "_fasttrack_to_absorb", lambda title: rec)
    monkeypatch.setattr(D, "_stage_of", lambda title: "committed")
    monkeypatch.setattr(D._autopilot, "get", lambda title: None)
    absorbed = []
    f = run(D.start({"title": "s", "steps": [AGENT, CMD], "start_on": "run"}, absorbed))
    assert absorbed == ["push"]
    assert [s["kind"] for s in f["steps"]] == ["ship", "agent", "command"]
    assert (f["state"], f["cursor"]) == ("ready", 0)
    assert {st["state"] for st in f["step_states"].values()} == {"pending"}


def test_driver_start_ready_creates_the_session_with_no_prompt(srv, monkeypatch):
    import backend.web.core.session_create as sc

    created = []

    async def fake_create(payload, **kw):
        created.append(payload)
        srv.ENGINE.instances["new-1"] = FakeInst(payload["repo_path"])
        return 202, {"title": "new-1"}

    monkeypatch.setattr(sc, "create_result", fake_create)
    f = run(
        D.start(
            {
                "steps": [AGENT],
                "repo_path": "/r",
                "prompt": "typed?",
                "start_on": "prompt",
            }
        )
    )
    assert created == [{"repo_path": "/r", "prompt": ""}]
    assert (f["session"], f["state"]) == ("new-1", "ready")
    assert f["step_states"]["s1"] == F._blank_step_state()


def test_driver_attach_ready(srv, monkeypatch):
    import backend.web.core.session_create as sc

    created = []

    async def fake_create(payload, **kw):
        created.append(payload)
        srv.ENGINE.instances["new-1"] = FakeInst(payload["repo_path"])
        return 202, {"title": "new-1"}

    monkeypatch.setattr(sc, "create_result", fake_create)
    g = run(D.ground({"steps": [AGENT]}))
    got = run(D.attach(g["id"], {"title": "s", "start_on": "prompt"}))
    assert (got["state"], got["session"], got["start_on"]) == ("ready", "s", "prompt")
    h = run(D.ground({"steps": [AGENT]}))
    got = run(D.attach(h["id"], {"title": "", "repo_path": "/q", "start_on": "run"}))
    assert created == [{"repo_path": "/q", "prompt": ""}]
    assert got["state"] == "ready" and got["step_states"]["s1"]["detail"] == {}


# --------------------------------------------------------------------------- #
# Run and PATCH start_on
# --------------------------------------------------------------------------- #
def test_run_route_takes_off_a_ready_flight(engine):
    f = ready(CMD, NOTIFY, start_on="run")
    r = client.post("/api/flights/%s/run" % f["id"])
    assert r.status_code == 200
    got = r.json()["flight"]
    assert (got["state"], got["cursor"]) == ("running", 0)
    assert got["history"][-1]["event"] == "took_off"
    assert got["history"][-1]["by"] == "run"
    # Run never waits for a turn.
    assert got["step_states"]["s1"]["detail"] == {}
    r = client.post("/api/flights/%s/run" % f["id"])
    assert (r.status_code, r.json()["error"]) == (409, "not_ready")
    r = client.post("/api/flights/f_nope/run")
    assert (r.status_code, r.json()["error"]) == (404, "unknown_flight")


def test_run_stops_at_a_halt_point_on_the_first_stop():
    f = ready(dict(CMD, halt=True), start_on="run")
    got = F.mutate(f["id"], lambda x: F.op_take_off(x, "run", NOW))
    assert (got["state"], got["reason"]) == ("paused", F.HALT_POINT)


def test_patch_start_on(engine):
    f = ready(CMD, start_on="prompt")
    r = client.patch("/api/flights/" + f["id"], json={"start_on": "run"})
    assert r.status_code == 200
    got = r.json()["flight"]
    assert (got["state"], got["start_on"]) == ("ready", "run")
    assert got["history"][-1]["event"] == "edited"
    r = client.patch("/api/flights/" + f["id"], json={"start_on": "bogus"})
    assert (r.status_code, r.json()["error"]) == (400, "bad_start_on")
    r = client.patch("/api/flights/" + f["id"], json={"start_on": None})
    assert (r.status_code, r.json()["error"]) == (400, "bad_start_on")
    # rename + start_on together
    r = client.patch(
        "/api/flights/" + f["id"], json={"plan_name": "Shots", "start_on": "prompt"}
    )
    assert r.status_code == 200
    assert (r.json()["flight"]["plan_name"], r.json()["flight"]["start_on"]) == (
        "Shots",
        "prompt",
    )
    # "now" on a ready flight runs it
    r = client.patch("/api/flights/" + f["id"], json={"start_on": "now"})
    got = r.json()["flight"]
    assert got["state"] == "running" and got["history"][-1]["by"] == "run"
    r = client.patch("/api/flights/" + f["id"], json={"start_on": "run"})
    assert (r.status_code, r.json()["error"]) == (409, "not_ready")
    # plan_name alone is today's rename
    r = client.patch("/api/flights/" + f["id"], json={"plan_name": "Again"})
    assert r.status_code == 200 and r.json()["flight"]["plan_name"] == "Again"


def test_start_route_passes_start_on(engine):
    r = client.post(
        "/api/flights/start",
        json={"title": "s", "steps": [CMD], "start_on": "prompt"},
    )
    assert r.status_code == 202
    assert r.json()["flight"]["state"] == "ready"
    r = client.post(
        "/api/flights/start", json={"title": "s", "steps": [CMD], "start_on": "x"}
    )
    assert (r.status_code, r.json()["error"]) == (400, "bad_start_on")


def test_attach_route_passes_start_on(engine):
    g = F.ground(None, [CMD], now=NOW)
    r = client.post(
        "/api/flights/%s/attach" % g["id"], json={"title": "s", "start_on": "run"}
    )
    assert r.status_code == 200
    assert (r.json()["flight"]["state"], r.json()["flight"]["start_on"]) == (
        "ready",
        "run",
    )


# --------------------------------------------------------------------------- #
# Taking off on the next prompt
# --------------------------------------------------------------------------- #
def test_on_prompt_takes_off_only_a_prompt_ready_flight():
    assert F.on_prompt("s", NOW + 10) is None  # no flight
    f = ready(CMD, start_on="run")
    assert F.on_prompt("s", NOW + 10) is None
    assert F.get(f["id"])["state"] == "ready"
    g = ready(CMD, NOTIFY, session="t")
    before, after = F.on_prompt("t", NOW + 10)
    assert before["state"] == "ready"
    assert (after["state"], after["cursor"]) == ("running", 0)
    assert after["step_states"]["s1"]["detail"] == {
        "after_turn": True,
        "turn_from": NOW + 10,
    }
    assert after["history"][-1]["event"] == "took_off"
    assert after["history"][-1]["by"] == "prompt"
    assert F.get(g["id"])["state"] == "running"
    # A second prompt changes nothing: it is no longer ready.
    assert F.on_prompt("t", NOW + 20) is None


def test_driver_on_prompt_announces_and_wakes(monkeypatch):
    woke, said = [], []
    monkeypatch.setattr(D, "wake", lambda: woke.append(1))
    monkeypatch.setattr(
        D, "_announce", lambda b, a: said.append((b["state"], a["state"]))
    )
    assert D.on_prompt("s") is None and woke == []
    ready(CMD)
    assert D.on_prompt("s")["state"] == "running"
    assert woke == [1] and said == [("ready", "running")]


@pytest.fixture
def send(monkeypatch, engine):
    sent = []

    def deliver(inst, title, text, **kw):
        sent.append((title, text))
        return "sent", None

    monkeypatch.setattr(server, "_deliver_to_agent", deliver)
    monkeypatch.setattr(server, "_driven_elsewhere", lambda req, title, payload: None)
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    monkeypatch.setattr(server, "_flock_typed", lambda inst, title, one: False)
    monkeypatch.setattr(server._sleep, "is_sleeping", lambda title: False)
    return sent


def test_the_send_route_takes_off_a_ready_chain(send):
    f = ready(CMD)
    # A paste that is not submitted is not a prompt.
    r = client.post("/api/instances/s/send", json={"text": "draft", "submit": False})
    assert r.status_code == 200 and F.get(f["id"])["state"] == "ready"
    r = client.post("/api/instances/s/send", json={"text": "go on"})
    assert r.status_code == 200 and send[-1] == ("s", "go on")
    got = F.get(f["id"])
    assert got["state"] == "running"
    assert got["step_states"]["s1"]["detail"]["after_turn"] is True


def test_a_queued_send_waits_for_the_drain(send, monkeypatch):
    monkeypatch.setattr(
        server, "_deliver_to_agent", lambda inst, title, text, **kw: ("queued", None)
    )
    f = ready(CMD)
    r = client.post("/api/instances/s/send", json={"text": "later"})
    assert r.json()["queued"] is True
    assert F.get(f["id"])["state"] == "ready"


def test_the_queue_drain_takes_off_a_ready_chain(monkeypatch):
    fake = SimpleNamespace(
        ENGINE=SimpleNamespace(instances={"s": FakeInst()}),
        _send_to_agent=lambda name, text, submit=True: True,
        _voice_say=lambda inst, title, text: text,
        _voice_say_cancel=lambda title: None,
    )
    monkeypatch.setattr(Q, "_server", lambda: fake)
    monkeypatch.setattr(Q, "_dialog_on_screen", lambda inst, name: False)
    monkeypatch.setattr(
        Q._prompt_queue,
        "record_sent",
        lambda title, iid: {"items": [], "loop": False},
    )
    f = ready(CMD)
    rec = {}
    assert Q._send_queued_item("s", "mf_s", {"id": "q1", "text": "next"}, rec, NOW)
    assert F.get(f["id"])["state"] == "running"


def test_the_mailbox_lane_takes_off_a_ready_chain(monkeypatch):
    """MCP send_message lands in the mailbox; its lane (and delivery "now")
    types it through ``_mail_type``."""
    fake = SimpleNamespace(_send_to_agent=lambda name, text, submit=True: True)
    monkeypatch.setattr(Q, "_server", lambda: fake)
    monkeypatch.setattr(Q, "_dialog_on_screen", lambda inst, name: False)
    monkeypatch.setattr(Q, "_mail_provider_name", lambda inst: "claude")
    monkeypatch.setattr(
        Q._mailbox, "render_delivery", lambda msg, prov: ("[msg] hi", True)
    )
    monkeypatch.setattr(Q._mailbox, "claim", lambda *a, **kw: {"id": "m1"})
    f = ready(CMD)
    got = Q._mail_type(FakeInst(), "s", "mf_s", {"id": "m1", "from": "w"}, NOW)
    assert got == "delivered"
    assert F.get(f["id"])["state"] == "running"


# --------------------------------------------------------------------------- #
# The tick catches a turn typed into the terminal
# --------------------------------------------------------------------------- #
def test_ready_turn_started():
    f = ready(CMD)
    assert F.ready_turn_started(f, {"activity": "working", "activity_since": NOW + 1})
    # a turn already under way when it became ready does not count
    assert not F.ready_turn_started(
        f, {"activity": "working", "activity_since": NOW - 5}
    )
    assert not F.ready_turn_started(f, {"activity": "working", "activity_since": NOW})
    assert not F.ready_turn_started(f, {"activity": "idle", "activity_since": NOW + 1})
    assert not F.ready_turn_started(f, {})
    g = dict(f, start_on="run")
    assert not F.ready_turn_started(
        g, {"activity": "working", "activity_since": NOW + 1}
    )


def test_the_tick_takes_off_on_a_typed_turn(srv, monkeypatch):
    stepped = []

    async def fake_step(fid):
        stepped.append(fid)

    monkeypatch.setattr(D, "step_flight", fake_step)
    monkeypatch.setattr(D, "wake", lambda: None)
    f = ready(CMD)
    row = {"activity": "working", "activity_since": NOW - 30}
    monkeypatch.setattr(D, "_row", lambda title: row)
    run(D.run_pass())
    # the turn under way when the chain became ready: nothing happens
    assert F.get(f["id"])["state"] == "ready" and stepped == []
    row.update(activity="idle", activity_since=NOW + 40)
    run(D.run_pass())
    assert F.get(f["id"])["state"] == "ready"
    row.update(activity="working", activity_since=NOW + 60)
    run(D.run_pass())
    got = F.get(f["id"])
    assert got["state"] == "running" and stepped == [f["id"]]
    assert got["step_states"]["s1"]["detail"]["after_turn"] is True
    assert got["history"][-1]["by"] == "prompt"


# --------------------------------------------------------------------------- #
# The after_turn gate
# --------------------------------------------------------------------------- #
def _took_off(*steps, at=NOW + 100):
    f = ready(*steps)
    return F.on_prompt("s", at)[1]


def test_after_turn_holds_the_first_stop_until_the_turn_ends():
    f = _took_off(CMD, NOTIFY)
    t = NOW + 100
    # the prompt has not shown up as work yet
    a = step_once(f, sess("idle"), t + 1)
    assert a["action"] == "wait" and a["note"].startswith("after your turn")
    # the agent works on the prompt
    a = step_once(f, sess("working"), t + 5)
    assert a["action"] == "wait" and "working" in a["note"]
    assert f["step_states"]["s1"]["detail"]["worked_at"] == t + 5
    # it goes idle: the settle window
    a = step_once(f, sess("idle"), t + 60)
    assert a["action"] == "wait"
    a = step_once(f, sess("idle"), t + 60 + F.IDLE_SETTLE_S - 1)
    assert a["action"] == "wait"
    # the turn is over: the first stop runs
    a = step_once(f, sess("idle"), t + 61 + F.IDLE_SETTLE_S)
    assert a["action"] == "run"
    assert f["step_states"]["s1"]["state"] == "running"
    assert "after_turn" not in f["step_states"]["s1"]["detail"]


def test_a_turn_that_ended_between_passes_counts():
    f = _took_off(CMD)
    t = NOW + 100
    a = step_once(f, sess("idle", turn_ended_at=t + 3), t + 4)
    assert a["action"] == "wait"
    a = step_once(f, sess("idle", turn_ended_at=t + 3), t + 5 + F.IDLE_SETTLE_S)
    assert a["action"] == "run"


def test_a_question_for_you_ends_the_turn():
    f = _took_off(CMD)
    t = NOW + 100
    step_once(f, sess("working"), t + 2)
    a = step_once(f, sess("clarify"), t + 3)
    assert a["action"] == "run"


def test_an_empty_agent_stop_counts_as_your_prompt():
    f = _took_off(EMPTY_AGENT, CMD)
    t = NOW + 100
    step_once(f, sess("working"), t + 2)
    step_once(f, sess("idle"), t + 10)
    a = step_once(f, sess("idle"), t + 11 + F.IDLE_SETTLE_S)
    assert a["action"] == "done"
    ss = f["step_states"]["s1"]
    assert ss["state"] == "done" and ss["detail"]["receipt"] == F.YOUR_PROMPT
    assert f["cursor"] == 1 and f["state"] == "running"
    # the next stop then runs on its own
    assert step_once(f, sess("idle"), t + 12 + F.IDLE_SETTLE_S)["action"] == "run"


def test_an_agent_stop_with_a_prompt_runs_after_the_turn():
    f = _took_off(AGENT)
    t = NOW + 100
    step_once(f, sess("working"), t + 2)
    step_once(f, sess("idle"), t + 10)
    a = step_once(f, sess("idle"), t + 11 + F.IDLE_SETTLE_S)
    assert a["action"] == "run"  # its own prompt is typed after your turn


def test_a_prompt_that_never_starts_a_turn_stops_holding():
    f = _took_off(CMD)
    t = NOW + 100
    step_once(f, sess("idle"), t + 1)
    a = step_once(f, sess("idle"), t + 1 + F.IDLE_SETTLE_S + 1)
    assert a["action"] == "wait" and "start" in a["note"]
    a = step_once(f, sess("idle"), t + F.AFTER_TURN_START_S + 5)
    assert a["action"] == "run"


def test_run_never_waits_for_a_turn():
    f = ready(CMD, start_on="prompt")
    f = F.mutate(f["id"], lambda x: F.op_take_off(x, "run", NOW + 1))
    a = step_once(f, sess("working"), NOW + 2)
    assert a["action"] == "run"


def test_observe_reads_agent_facts_for_the_gate(srv, monkeypatch):
    seen = []
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: seen.append(agent) or sess("idle"),
    )
    f = _took_off(CMD)
    run(D.observe(f, NOW + 101))
    assert seen == [True]


# --------------------------------------------------------------------------- #
# Defaults unchanged: Fly, schedules, triggers, handoff, MCP start_flight
# --------------------------------------------------------------------------- #
def test_start_without_start_on_flies_at_once(srv):
    f = run(D.start({"title": "s", "steps": [CMD]}))
    assert (f["state"], f["start_on"]) == ("running", "now")


def test_attach_without_start_on_flies_at_once(srv):
    g = run(D.ground({"steps": [CMD]}))
    assert run(D.attach(g["id"], {"title": "s"}))["state"] == "running"


def test_arm_pending_flies_at_once():
    f = F.arm_pending("ticket-1", plan_of(AGENT, CMD)["id"], {"kind": "manual"})
    assert f["state"] == "running" and f["start_on"] == "now"


def test_mcp_control_flight_has_run_and_start_flight_sends_start_on_only_when_asked():
    assert "run" in mcp_flights.FLIGHT_OPS and "run" in mcp_flights.CONTROL_OPS
    assert "run" in mcp_flights.D_CONTROL
    assert "ready" in mcp_flights.D_STATUS
    out = mcp_flights.compact_flight(ready(CMD), NOW)
    assert out["state"] == "ready" and out["start_on"] == "prompt"
    import inspect

    # An agent may ask for start_on ("prompt" / "run": the route waits for the
    # agent's work — docs/vision/mcp-routes.md); a plain start sends none
    # (test_mcp_flights pins the default payload).
    src = inspect.getsource(mcp_flights.FlightTools.start_flight)
    assert 'args.get("start_on") in ("prompt", "run")' in src
