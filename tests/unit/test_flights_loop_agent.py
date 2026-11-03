"""A Loop back to the session's AGENT (docs/flights-loop-agent.md):
``back_to: "agent"`` + ``prompt`` — config, the pure judge and jump, and the
driver flying "Notify → Loop back to the agent" against tmp stores and a fake
server (never the real engine or ``~/.mindflock``)."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import loop as L

NOW = time.time()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_JOBS", {})
    # The session's newest prompt: none unless a test says so.
    monkeypatch.setattr(L, "_last_prompt", lambda title: "")
    yield


def fly(*steps, **kw):
    plan = F.save_plan({"name": "P", "steps": list(steps)})
    kw.setdefault("session", "s")
    return F.take_off(plan, **kw)


def run(coro):
    return asyncio.run(coro)


NOTIFY = {"kind": "notify", "config": {"text": "ping"}}


def LOOP(**cfg):
    return {"kind": "loop", "config": dict({"back_to": "agent"}, **cfg)}


def _at_loop(f, runs=0, last_start=None):
    """``f`` with every stop before the (last) loop done and the loop running."""
    i = len(f["steps"]) - 1
    for s in f["steps"][:i]:
        f["step_states"][s["id"]].update(state="done", since=NOW - 3600)
    f["cursor"] = i
    ss = f["step_states"][f["steps"][i]["id"]]
    ss.update(state="running", since=NOW - 10)
    ss["detail"] = {"runs": runs}
    if last_start is not None:
        ss["detail"]["last_start"] = last_start
    return f


def judge(f, obs=None, now=NOW):
    obs = obs if obs is not None else {"session": {"present": True}}
    return F.next_action(f, obs, now)


# --------------------------------------------------------------------------- #
# Config and validation
# --------------------------------------------------------------------------- #
def test_back_to_agent_is_kept_as_is():
    step = F.normalize_step(LOOP(prompt="go again", times=5))
    assert step["config"]["back_to"] == "agent"
    assert step["config"]["prompt"] == "go again"
    assert F.loops_to_agent(step)
    assert not F.loops_to_agent(F.normalize_step({"kind": "loop", "config": {}}))


def test_the_prompt_is_at_most_4000_characters():
    F.normalize_step(LOOP(prompt="x" * 4000))
    with pytest.raises(F.PlanError) as err:
        F.normalize_step(LOOP(prompt="x" * 4001))
    assert err.value.field == "steps[0].config.prompt"
    lenient = F.normalize_step(LOOP(prompt="x" * 5000), config_strict=False)
    assert len(lenient["config"]["prompt"]) == 4000


def test_the_kinds_catalog_has_the_prompt_field():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "loop")
    fields = {f["name"]: f for f in row["fields"]}
    assert fields["prompt"]["label"] == "Prompt to send the agent"
    assert fields["prompt"]["default"] == ""
    assert "agent" in row["summary"]


def test_ok_on_a_session_flight():
    f = fly(NOTIFY, LOOP())
    assert f["steps"][1]["config"]["back_to"] == "agent"
    F.check_agent_loops(f)  # no refusal
    assert D.needs_agent(f) and D.needs_session(f)


def test_refused_on_a_stops_only_chain():
    g = F.ground(None, [NOTIFY, LOOP()], now=NOW)
    # Grounded: it may still get a session, so it is kept…
    F.check_agent_loops(g)
    # …but bound to a folder (no session) it is refused, by field.
    with pytest.raises(F.PlanError) as err:
        F.attach(g["id"], "", "/tmp", "")
    assert err.value.field == "steps[1].config.back_to"
    assert "needs a session" in err.value.message
    assert F.get(g["id"])["state"] == F.GROUNDED


def test_adding_one_to_a_free_chain_in_the_air_is_refused(monkeypatch):
    monkeypatch.setattr(D, "_emit", lambda *a, **k: None)
    g = F.ground(None, [{"kind": "wait", "config": {"seconds": 3600}}], now=NOW)
    free = F.attach(g["id"], "", "/tmp", "")
    assert free.get("free") and free["state"] == "running"
    with pytest.raises(F.PlanError) as err:
        D.change_steps(free["id"], "add", raw=LOOP())
    assert err.value.field.endswith(".config.back_to")
    assert len(F.get(free["id"])["steps"]) == 1  # nothing written
    # A Loop back to a stop is fine there.
    D.change_steps(free["id"], "add", raw={"kind": "loop", "config": {}})


def test_first_on_the_chain_it_is_the_prompt_mode():
    """No stop before it: back to the agent IS repeating the session's prompt."""
    f = fly(LOOP(), NOTIFY)
    step = f["steps"][0]
    assert L.repeats_prompt(f, step) and not L.to_agent(f, step)
    g = fly(NOTIFY, LOOP(), session="t")
    assert L.to_agent(g, g["steps"][1]) and not L.repeats_prompt(g, g["steps"][1])


# --------------------------------------------------------------------------- #
# The judge and the jump (pure)
# --------------------------------------------------------------------------- #
def test_the_judge_sends_it_back_to_the_agent():
    out = judge(_at_loop(fly(NOTIFY, LOOP(times=5)), runs=1))
    assert (out["action"], out["phase"], out["detail"]["target"]) == (
        "run",
        "loop",
        "agent",
    )
    assert out["note"] == "run 3 of 5 — back to the agent"


def test_the_kind_refuses_to_run_with_no_session():
    f = _at_loop(fly(NOTIFY, LOOP()))
    f["session"] = ""
    step = f["steps"][1]
    ss = f["step_states"]["s2"]
    out = L._judge(f, step, ss, {"session": {}}, NOW)
    assert out == {"action": "fail", "reason": F.LOOP_AGENT_NO_SESSION}


def test_times_exhausts():
    out = judge(_at_loop(fly(NOTIFY, LOOP(times=3)), runs=2))
    assert out["action"] == "done"
    assert out["detail"]["receipt"] == "looped 3 times"


def test_every_paces_the_repeats():
    f = _at_loop(fly(NOTIFY, LOOP(times=0, every_seconds=3600)), last_start=NOW - 60)
    out = judge(f)
    assert out["action"] == "wait" and out["note"].startswith("next run in")
    assert out["detail"]["next_at"] == pytest.approx(NOW - 60 + 3600)
    assert judge(f, now=NOW + 3600)["action"] == "run"


def test_loop_back_to_the_agent_resets_and_gates_stop_0():
    f = _at_loop(fly(NOTIFY, {"kind": "wait", "config": {"seconds": 5}}, LOOP()))
    f["state"] = "running"
    F.apply(f, {"action": "loop", "to": "agent", "sent_at": NOW, "note": ""}, NOW)
    assert f["cursor"] == 0 and f["state"] == "running"
    for sid in ("s1", "s2"):
        assert f["step_states"][sid]["state"] == "pending"
    assert f["step_states"]["s1"]["detail"] == {"after_turn": True, "turn_from": NOW}
    assert f["step_states"]["s2"]["detail"] == {}
    loop = f["step_states"]["s3"]
    assert loop["state"] == "running"
    assert (loop["detail"]["runs"], loop["detail"]["target"]) == (1, "agent")
    assert f["history"][-1]["event"] == "looped"
    assert f["history"][-1]["note"] == "run 2 of 3 — back to the agent"


def test_stop_0_waits_for_the_turn_then_flies():
    f = _at_loop(fly(NOTIFY, LOOP()))
    f["state"] = "running"
    F.apply(f, {"action": "loop", "to": "agent", "sent_at": NOW, "note": ""}, NOW)
    busy = {"present": True, "activity": "working"}
    assert judge(f, {"session": busy}, NOW + 5)["action"] == "wait"


# --------------------------------------------------------------------------- #
# The driver
# --------------------------------------------------------------------------- #
class FakeInst:
    Program = "claude"
    Path = "/repo"
    Branch = "feat"
    Status = SimpleNamespace(name="Running")

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
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    monkeypatch.setattr(D, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(D, "_announce", lambda *a, **k: None)
    return ns


@pytest.fixture
def agent(monkeypatch):
    """The session's agent as the driver reads it: ``auto`` = every turn has
    ended (its turn_ended_at always ahead), else what the test sets."""
    state = {"auto": True, "activity": "idle", "turn_ended_at": 0.0, "limited": False}

    def fake(flight, now, agent_):
        out = {
            "present": flight["session"] == "s",
            "pending": False,
            "status": "running",
            "sleeping": False,
            "activity": state["activity"],
            "branch": "feat",
            "folder": "/repo",
            "pr_url": "",
        }
        if agent_:
            out["limited"] = state["limited"]
            out["turn_ended_at"] = (
                time.time() + 10**6 if state["auto"] else state["turn_ended_at"]
            )
        return out

    monkeypatch.setattr(D, "_session_obs", fake)
    monkeypatch.setattr(F, "IDLE_SETTLE_S", 0.0)
    return state


@pytest.fixture
def sent(monkeypatch):
    got = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: got.append((title, text)))
    return got


@pytest.fixture
def pings(monkeypatch):
    got = []
    monkeypatch.setattr(D, "_run_notify", lambda flight, step: got.append(step["id"]))
    return got


def _drive(fid, passes=40):
    for _ in range(passes):
        f = F.get(fid)
        if f["state"] not in F.ACTIVE:
            return f
        run(D.step_flight(fid))
    return F.get(fid)


def test_the_whole_loop_prompts_the_agent_then_flies_the_chain(srv, agent, sent, pings):
    f = fly(NOTIFY, LOOP(times=3, prompt="tidy the sky"))
    got = _drive(f["id"])
    assert got["state"] == "done"
    assert sent == [("s", "tidy the sky")] * 2
    assert pings == ["s1"] * 3
    loop = got["step_states"]["s2"]
    assert loop["state"] == "done" and loop["detail"]["runs"] == 2
    assert loop["detail"]["receipt"] == "looped 3 times"
    looped = [h["note"] for h in got["history"] if h["event"] == "looped"]
    assert looped == [
        "run 2 of 3 — back to the agent",
        "run 3 of 3 — back to the agent",
    ]


def test_stop_0_does_not_fly_until_the_turn_ends(srv, agent, sent, pings):
    agent["auto"] = False
    f = fly(NOTIFY, LOOP(times=2, prompt="again"))
    got = _drive(f["id"], passes=6)
    # Run 1 flew the Notify; the repeat sent the prompt and went back to 0…
    assert sent == [("s", "again")] and pings == ["s1"]
    assert got["cursor"] == 0 and got["state"] == "running"
    assert got["step_states"]["s1"]["state"] == "pending"
    assert got["step_states"]["s2"]["state"] == "running"
    # …where it waits: the agent has not started (nor ended) that turn.
    assert "after your turn" in got["step_states"]["s1"]["note"]
    agent["activity"] = "working"
    got = _drive(f["id"], passes=4)
    assert pings == ["s1"] and got["cursor"] == 0
    assert "after your turn" in got["step_states"]["s1"]["note"]
    # The turn ends: stop 0 flies, then the Loop is through.
    agent["activity"] = "idle"
    agent["turn_ended_at"] = time.time() + 1
    got = _drive(f["id"])
    assert pings == ["s1", "s1"]
    assert got["state"] == "done"
    assert sent == [("s", "again")]


def test_fresh_clears_the_context_first(srv, agent, sent, pings, monkeypatch):
    monkeypatch.setattr(L, "_clear_command", lambda title: "/clear")
    f = fly(NOTIFY, LOOP(times=2, prompt="again", fresh=True))
    assert _drive(f["id"])["state"] == "done"
    assert sent == [("s", "/clear"), ("s", "again")]


def test_no_prompt_of_its_own_resends_the_sessions_prompt(
    srv, agent, sent, pings, monkeypatch
):
    seen = iter(["polish the sky"])
    monkeypatch.setattr(L, "_last_prompt", lambda title: next(seen, "later prompt"))
    f = fly(NOTIFY, LOOP(times=3))
    got = _drive(f["id"])
    assert got["state"] == "done"
    # The prompt the Loop saw on its first arrival, every repeat.
    assert sent == [("s", "polish the sky")] * 2
    assert got["step_states"]["s2"]["detail"]["agent_prompt"] == "polish the sky"


def test_no_prompt_known_halts_with_the_note(srv, agent, sent, pings):
    f = fly(NOTIFY, LOOP(times=3))
    got = _drive(f["id"])
    assert got["state"] == "halted"
    assert got["reason"] == "Loop back to the agent needs a prompt"
    assert got["step_states"]["s2"]["state"] == "failed"
    assert sent == []


def test_every_paces_the_driver(srv, agent, sent, pings):
    f = fly(NOTIFY, LOOP(times=2, prompt="again", every_seconds=1800))
    got = _drive(f["id"], passes=8)
    assert got["state"] == "running" and got["cursor"] == 1
    assert got["step_states"]["s2"]["note"] == "next run in 30 min"
    assert sent == []


def test_waits_out_the_usage_limit(srv, agent, sent, pings):
    agent["limited"] = True
    f = fly(NOTIFY, LOOP(times=2, prompt="again"))
    got = _drive(f["id"], passes=8)
    assert got["step_states"]["s2"]["note"] == "waiting on the usage limit"
    assert sent == []
    agent["limited"] = False
    assert _drive(f["id"])["state"] == "done"
    assert sent == [("s", "again")]


def test_the_flight_json_carries_the_config(tmp_path, monkeypatch):
    from backend.web import server

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(server.ENGINE, "instances", {"sess": FakeInst()})
    monkeypatch.setattr(D, "wake", lambda: None)
    client = TestClient(server.app)
    r = client.post(
        "/api/flights/start",
        json={"title": "sess", "steps": [NOTIFY, LOOP(times=0, prompt="go")]},
    )
    assert r.status_code == 202, r.text
    fid = r.json()["flight"]["id"]
    view = client.get("/api/flights/" + fid).json()["flight"]
    cfg = view["steps"][1]["config"]
    assert (cfg["back_to"], cfg["prompt"], cfg["times"]) == ("agent", "go", 0)
    r = client.post(
        "/api/flights/start",
        json={"title": "sess2", "steps": [NOTIFY, LOOP(prompt="x" * 4001)]},
    )
    assert r.status_code == 400, r.text
