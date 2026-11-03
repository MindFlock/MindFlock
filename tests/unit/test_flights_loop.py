"""The Loop stop (docs/flights.md "Round 16"): ``flight_kinds/loop.py`` (config,
title, the pure judge, the run) and the core's ``loop_back`` — against tmp
stores and a fake server, never the real engine or ``~/.mindflock``."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.providers.base import BaseProvider
from backend.providers.claude import ClaudeProvider
from backend.providers.codex import CodexProvider
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import loop as L

NOW = time.time()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_JOBS", {})
    yield


def fly(*steps, **kw):
    plan = F.save_plan({"name": "P", "steps": list(steps)})
    kw.setdefault("session", "s")
    return F.take_off(plan, **kw)


def run(coro):
    return asyncio.run(coro)


AGENT = {"kind": "agent", "config": {"prompt": "improve the sky"}}


def _at_loop(f, runs=0, last_start=None, i=None):
    """``f`` with every stop before the loop done and the loop running."""
    i = len(f["steps"]) - 1 if i is None else i
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
# The kind: spec, config, title
# --------------------------------------------------------------------------- #
def test_the_dto_row():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "loop")
    assert (row["label"], row["family"], row["actor"], row["default_on_fail"]) == (
        "Loop",
        "work",
        "robot",
        "halt",
    )
    fields = {f["name"]: f for f in row["fields"]}
    assert list(fields) == [
        "back_to",
        "times",
        "every_seconds",
        "wait_limits",
        "fresh",
        "until",
        "prompt",
    ]
    assert fields["back_to"] == {
        "name": "back_to",
        "type": "select",
        "label": "Repeat from",
        "options": [],
        "default": "",
        "dynamic": "stops_before",
    }
    assert fields["times"]["default"] == 3 and fields["every_seconds"]["default"] == 0
    assert fields["every_seconds"]["type"] == "duration"
    assert fields["wait_limits"]["default"] is True
    assert fields["fresh"]["default"] is False
    assert fields["until"]["type"] == "text"
    assert "loop" in F.SESSION_KINDS and "loop" not in F.AGENT_KINDS


@pytest.mark.parametrize(
    "config,title",
    [
        ({}, "Loop ×3"),
        ({"times": 3, "every_minutes": 30}, "Loop ×3 every 30 min"),
        ({"times": 5, "every_minutes": 120}, "Loop ×5 every 2 h"),
        ({"until": "JUDGE IS yes"}, "Loop until judge is yes"),
        ({"times": 0}, "Loop forever"),
        ({"times": 0, "every_minutes": 15}, "Loop every 15 min"),
    ],
)
def test_default_title(config, title):
    assert F.normalize_step({"kind": "loop", "config": config})["title"] == title


@pytest.mark.parametrize(
    "config,field",
    [
        ({"until": "the moon is full"}, "steps[0].config.until"),
        ({"back_to": "no spaces allowed"}, "steps[0].config.back_to"),
        ({"times": -1}, "steps[0].config.times"),
        ({"times": 1001}, "steps[0].config.times"),
        ({"every_minutes": 7 * 24 * 60 + 1}, "steps[0].config.every_seconds"),
    ],
)
def test_strict_config_names_the_bad_field(config, field):
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "loop", "config": config})
    assert err.value.field == field


def test_lenient_config_keeps_a_placed_block_incomplete():
    step = F.normalize_step(
        {
            "kind": "loop",
            "config": {"until": "judge is", "times": -4, "back_to": "x y"},
        },
        config_strict=False,
    )
    assert step["config"]["until"] == "judge is"  # kept as typed, fixed later
    assert step["config"]["times"] == 0 and step["config"]["back_to"] == ""
    with pytest.raises(F.PlanError):
        F.validate_for_takeoff({"steps": [step]})


# --------------------------------------------------------------------------- #
# The judge (pure)
# --------------------------------------------------------------------------- #
def test_first_arrival_runs_then_the_judge_sends_the_flight_back():
    f = fly(AGENT, {"kind": "loop", "config": {"times": 3}})
    f["cursor"] = 1
    f["step_states"]["s1"].update(state="done", since=NOW - 60)
    first = judge(f)
    assert first["action"] == "run" and "phase" not in first  # the core starts it
    out = judge(_at_loop(f, runs=0))
    assert out["action"] == "run" and out["phase"] == "loop"
    assert out["note"] == "run 2 of 3 — back to Agent works"
    assert out["detail"] == {"target": "s1"}


def test_done_by_count_with_its_receipt():
    f = _at_loop(fly(AGENT, {"kind": "loop", "config": {"times": 3}}), runs=2)
    out = judge(f)
    assert out["action"] == "done"
    assert out["detail"]["receipt"] == "looped 3 times" == out["note"]
    one = _at_loop(
        fly(AGENT, {"kind": "loop", "config": {"times": 1}}, session="t"), runs=0
    )
    assert judge(one)["detail"]["receipt"] == "looped 1 time"


def test_times_zero_never_ends_by_count():
    f = _at_loop(fly(AGENT, {"kind": "loop", "config": {"times": 0}}), runs=500)
    out = judge(f)
    assert out["action"] == "run" and out["note"] == "run 502 — back to Agent works"


def test_done_by_until_whatever_times_says():
    f = _at_loop(
        fly(AGENT, {"kind": "loop", "config": {"times": 0, "until": "files < 1"}})
    )
    # The facts are gathered by observe; until then the judge waits.
    assert judge(f)["note"] == "checking files < 1"
    obs = {"session": {"present": True}, "loop": {"facts": {"files": ["a.py"]}}}
    assert judge(f, obs)["action"] == "run"
    obs["loop"]["facts"]["files"] = []
    out = judge(f, obs)
    assert out["action"] == "done"
    assert out["detail"]["receipt"] == "stopped by: files < 1"


def test_an_unreadable_until_fails_the_stop():
    f = fly(AGENT, {"kind": "loop", "config": {}})
    f["steps"][1]["config"]["until"] = "judge is"  # stored lenient
    out = judge(_at_loop(f), {"session": {"present": True}, "loop": {"facts": {}}})
    assert out["action"] == "fail" and out["reason"].startswith("until: ")
    assert out["then"] == "halt"


def test_waits_on_the_usage_limit_unless_told_not_to():
    f = _at_loop(fly(AGENT, {"kind": "loop", "config": {}}))
    obs = {"session": {"present": True}, "loop": {"limited": True}}
    out = judge(f, obs)
    assert out == {
        "action": "wait",
        "note": "waiting on the usage limit",
        "detail": {"next_at": None},
    }
    # The agent-row flag counts the same.
    assert judge(f, {"session": {"present": True, "limited": True}})["action"] == "wait"
    f["steps"][1]["config"]["wait_limits"] = False
    assert judge(f, obs)["action"] == "run"


def test_waits_for_the_interval():
    f = _at_loop(
        fly(AGENT, {"kind": "loop", "config": {"every_minutes": 30}}),
        last_start=NOW - 18 * 60,
    )
    out = judge(f)
    assert out["action"] == "wait" and out["note"] == "next run in 12 min"
    assert out["detail"]["next_at"] == pytest.approx(NOW + 12 * 60)
    assert judge(f, now=NOW + 12 * 60 + 1)["action"] == "run"


def test_the_first_runs_interval_counts_from_the_target_start():
    f = _at_loop(fly(AGENT, {"kind": "loop", "config": {"every_minutes": 90}}))
    # No last_start: run 1 began when s1 started (an hour ago).
    assert judge(f)["note"] == "next run in 30 min"


@pytest.mark.parametrize("back_to", ["s3", "nope"])
def test_a_bad_back_to_is_nothing_to_repeat(back_to):
    f = fly(AGENT, {"kind": "loop", "config": {"back_to": back_to}}, {"kind": "notify"})
    out = judge(_at_loop(f, i=1))
    assert (out["action"], out["reason"], out["then"]) == (
        "fail",
        "nothing to repeat",
        "halt",
    )


def test_a_loop_first_on_a_sessions_chain_repeats_the_sessions_prompt():
    """Nothing before it to go back to: it loops the session's own prompt."""
    f = fly({"kind": "loop", "config": {}}, AGENT)
    out = judge(_at_loop(f, i=0))
    assert (out["action"], out.get("phase"), out["detail"]["target"]) == (
        "run",
        "loop",
        "session",
    )
    # While the agent works on a run, it waits.
    assert (
        judge(_at_loop(f, i=0), {"session": {"present": True, "activity": "working"}})[
            "action"
        ]
        == "wait"
    )


def test_only_a_loop_first_on_a_sessions_chain_repeats_the_prompt():
    step = {"id": "s1", "kind": "loop", "config": {"back_to": ""}}
    assert L.repeats_prompt({"session": "s", "steps": [step]}, step)
    assert not L.repeats_prompt({"session": "", "steps": [step]}, step)
    aimed = {"id": "s2", "kind": "loop", "config": {"back_to": "s1"}}
    assert not L.repeats_prompt(
        {"session": "s", "steps": [{"id": "s1", "kind": "agent", "config": {}}, aimed]},
        aimed,
    )


def test_back_to_resolution():
    f = fly(AGENT, {"kind": "notify"}, AGENT, {"kind": "loop", "config": {}})
    loop = f["steps"][3]
    assert F.loop_target(f, loop) == 0
    assert F.loop_target(f, loop, "s2") == 1
    assert F.loop_target(f, loop, "s4") is None
    # In a group: "" is the group's first stop.
    g = {"id": "g1", "name": "Polish", "plan_id": ""}
    for s in f["steps"][2:]:
        s["group"] = dict(g)
    assert F.loop_target(f, loop) == 2


# --------------------------------------------------------------------------- #
# loop_back (core)
# --------------------------------------------------------------------------- #
def test_loop_back_resets_the_run_and_writes_history():
    f = fly(
        {"kind": "notify", "config": {"text": "before"}},
        AGENT,
        dict(AGENT, halt=True),
        {"kind": "loop", "config": {"back_to": "s2", "times": 3}},
        {"kind": "notify", "config": {"text": "after"}},
    )
    f = _at_loop(f, runs=0, i=3)
    f["step_states"]["s2"]["detail"] = {"halt_hit": True, "sent": True}
    f["step_states"]["s3"]["detail"] = {"halt_hit": True}
    configs = [dict(s["config"]) for s in f["steps"]]
    F.loop_back(f, "s4", 1, NOW)
    states = {sid: ss["state"] for sid, ss in f["step_states"].items()}
    assert states == {
        "s1": "done",
        "s2": "pending",
        "s3": "pending",
        "s4": "running",
        "s5": "pending",
    }
    assert [s["config"] for s in f["steps"]] == configs
    d = f["step_states"]["s4"]["detail"]
    assert (d["runs"], d["last_start"], d["target"]) == (1, NOW, "s2")
    assert f["cursor"] == 1 and f["state"] == "running"
    # The target is let through (as a jump onto it is); a halt point inside
    # the run is armed again.
    assert f["step_states"]["s2"]["detail"] == {}  # s2 has no halt
    assert f["step_states"]["s3"]["detail"] == {}
    assert F._armed_halt(f["steps"][2], f["step_states"]["s3"])
    h = f["history"][-1]
    assert (h["event"], h["step"], h["note"]) == (
        "looped",
        "s4",
        "run 2 of 3 — back to Agent works",
    )
    assert "looped" in F.HISTORY_EVENTS
    with pytest.raises(F.FlightError):
        F.loop_back(f, "s4", 3, NOW)


def test_loop_back_lets_a_halted_target_through():
    f = fly(dict(AGENT, halt=True), {"kind": "loop", "config": {}})
    f = _at_loop(f)
    F.loop_back(f, "s2", 0, NOW)
    assert f["step_states"]["s1"]["detail"] == {"halt_hit": True}
    assert F.next_action(f, {"session": {"present": True}}, NOW)["action"] != "hold"


def test_loop_history_survives_a_reload():
    f = fly(AGENT, {"kind": "loop", "config": {}})

    def fn(x):
        _at_loop(x)
        F.loop_back(x, "s2", 0, NOW)

    F.mutate(f["id"], fn)
    assert F.get(f["id"])["history"][-1]["event"] == "looped"


def test_skip_and_jump_need_no_special_casing():
    f = _at_loop(fly(AGENT, {"kind": "loop", "config": {}}, {"kind": "notify"}), i=1)
    f["state"] = "running"
    F.op_skip(f, "s2", NOW)
    assert f["cursor"] == 2 and f["step_states"]["s2"]["state"] == "skipped"
    g = _at_loop(fly(AGENT, {"kind": "loop", "config": {}}, session="t"), runs=2)
    F.op_jump(g, "s1", NOW)
    assert g["step_states"]["s2"]["detail"] == {}  # runs counted from 0 again


# --------------------------------------------------------------------------- #
# The driver: run, fresh delivery, the whole loop
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
        git_available=lambda: True,
        _peer_guard=SimpleNamespace(refusal=lambda inst, what: None),
        _lane_refusal=lambda title, lane: None,
        _publish_autopilot=lambda title: None,
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    return ns


@pytest.fixture
def row(monkeypatch):
    """A session whose agent always reads as settled (its turn ended in the
    future, so every step's ``since`` is behind it)."""
    state = {
        "activity": "idle",
        "branch": "feat",
        "folder": "/repo",
        "pr_url": "",
        "turn_ended_at": time.time() + 10**6,
        "limited": False,
    }

    def fake(flight, now, agent):
        out = {
            "present": flight["session"] == "s",
            "pending": False,
            "status": "running",
            "sleeping": False,
        }
        out.update(state)
        if not agent:
            out.pop("limited")
            out.pop("turn_ended_at")
        return out

    monkeypatch.setattr(D, "_session_obs", fake)
    monkeypatch.setattr(F, "IDLE_SETTLE_S", 0.0)
    return state


@pytest.fixture
def sent(monkeypatch):
    got = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: got.append((title, text)))
    return got


def _drive(fid, passes=30):
    for _ in range(passes):
        f = F.get(fid)
        if f["state"] not in F.ACTIVE:
            return f
        run(D.step_flight(fid))
    return F.get(fid)


def test_the_whole_loop_runs_the_prompt_three_times(srv, row, sent):
    f = fly(AGENT, {"kind": "loop", "config": {"times": 3}})
    got = _drive(f["id"])
    assert got["state"] == "done"
    assert sent == [("s", "improve the sky")] * 3
    loop = got["step_states"]["s2"]
    assert loop["state"] == "done" and loop["detail"]["runs"] == 2
    assert loop["detail"]["receipt"] == "looped 3 times"
    looped = [h["note"] for h in got["history"] if h["event"] == "looped"]
    assert looped == [
        "run 2 of 3 — back to Agent works",
        "run 3 of 3 — back to Agent works",
    ]


def test_fresh_clears_the_context_before_each_repeat(srv, row, sent):
    f = fly(AGENT, {"kind": "loop", "config": {"times": 2, "fresh": True}})
    got = _drive(f["id"])
    assert got["state"] == "done"
    assert sent == [
        ("s", "improve the sky"),
        ("s", "/clear"),
        ("s", "improve the sky"),
    ]


def test_fresh_with_no_clear_command_sends_nothing(srv, row, sent, monkeypatch):
    monkeypatch.setattr(L, "_provider_of", lambda title: BaseProvider())
    f = fly(AGENT, {"kind": "loop", "config": {"times": 2, "fresh": True}})
    assert _drive(f["id"])["state"] == "done"
    assert sent == [("s", "improve the sky")] * 2


def test_the_driver_waits_out_the_limit(srv, row, sent, monkeypatch):
    # Limited only once the flight is at the Loop (a limited agent never
    # settles, so the Agent stop would wait on it first).
    fake = D._session_obs
    limit = {"on": True}

    def obs(flight, now, agent):
        out = fake(flight, now, agent)
        if agent:
            out["limited"] = limit["on"] and flight["cursor"] == 1
        return out

    monkeypatch.setattr(D, "_session_obs", obs)
    f = fly(AGENT, {"kind": "loop", "config": {"times": 2}})
    got = _drive(f["id"], passes=8)
    assert got["state"] == "running" and got["cursor"] == 1
    assert got["step_states"]["s2"]["note"] == "waiting on the usage limit"
    limit["on"] = False
    assert _drive(f["id"])["state"] == "done"
    assert sent == [("s", "improve the sky")] * 2


def test_the_driver_waits_for_the_interval(srv, row, sent):
    f = fly(AGENT, {"kind": "loop", "config": {"times": 2, "every_minutes": 30}})
    got = _drive(f["id"], passes=8)
    assert got["state"] == "running" and got["cursor"] == 1
    assert got["step_states"]["s2"]["note"] == "next run in 30 min"
    assert sent == [("s", "improve the sky")]


def test_the_driver_repeats_the_sessions_prompt_for_a_loop_first_on_the_chain(
    srv, row, sent, monkeypatch
):
    """[Loop ×3, Agent] on a session: the session's last prompt goes again
    twice (the session's own turn was run 1), then the Agent stop flies."""
    from backend.web.core.flight_kinds import loop as L2

    monkeypatch.setattr(L2, "_last_prompt", lambda title: "polish the sky")
    f = fly({"kind": "loop", "config": {"times": 3}}, AGENT)
    got = _drive(f["id"], passes=60)
    assert [t for t in sent if t[1] == "polish the sky"] == [
        ("s", "polish the sky")
    ] * 2
    assert got["step_states"]["s1"]["state"] == "done"
    assert "your prompt 3 times" in got["step_states"]["s1"]["detail"].get(
        "receipt", ""
    )


def test_the_driver_halts_a_loop_with_nothing_to_repeat(srv, row, sent):
    f = fly(AGENT, {"kind": "loop", "config": {"back_to": "nope"}}, AGENT)
    got = _drive(f["id"])
    assert got["state"] == "halted" and got["reason"] == "nothing to repeat"


def test_removing_the_running_loop_pauses_as_usual(srv, row, sent, monkeypatch):
    f = fly(AGENT, {"kind": "loop", "config": {"times": 0, "every_minutes": 60}})
    _drive(f["id"], passes=6)
    assert F.get(f["id"])["cursor"] == 1
    out = D.change_steps(f["id"], "remove", "s2")
    assert (out["state"], out["reason"], out["cursor"]) == (
        "paused",
        F.STOP_REMOVED,
        1,
    )
    assert [s["id"] for s in out["steps"]] == ["s1"]


def test_providers_clear_commands():
    assert BaseProvider().clear_context_command() is None
    assert ClaudeProvider().clear_context_command() == "/clear"
    assert CodexProvider.clear_context_command(None) == "/new"


# --------------------------------------------------------------------------- #
# The route round-trip: POST /api/flights/start {title, steps}, then the driver
# --------------------------------------------------------------------------- #
def test_route_round_trip(tmp_path, monkeypatch, sent):
    from backend.web import server

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(server.ENGINE, "instances", {"sess": FakeInst()})
    client = TestClient(server.app)
    r = client.post(
        "/api/flights/start",
        json={
            "title": "sess",
            "steps": [AGENT, {"kind": "loop", "config": {"times": 2, "fresh": True}}],
        },
    )
    assert r.status_code == 202, r.text
    f = r.json()["flight"]
    assert [s["title"] for s in f["steps"]] == ["Agent works", "Loop ×2"]
    assert f["steps"][1]["config"] == {
        "back_to": "",
        "times": 2,
        "every_minutes": 0,
        "every_seconds": 0,
        "wait_limits": True,
        "fresh": True,
        "until": "",
        "prompt": "",
    }
    # Drive it with a settled fake session named like the real one.
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={"sess": FakeInst()}),
        _pending_rows=lambda: [],
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    monkeypatch.setattr(F, "IDLE_SETTLE_S", 0.0)
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: {
            "present": True,
            "pending": False,
            "status": "running",
            "sleeping": False,
            "activity": "idle",
            "branch": "b",
            "folder": "/repo",
            "turn_ended_at": time.time() + 10**6,
            "limited": False,
        },
    )
    # An attached start: the session is already doing the Agent stop (the
    # cursor starts on the Loop), so only the repeat is typed.
    assert f["cursor"] == 1
    got = _drive(f["id"])
    assert got["state"] == "done"
    assert sent == [("sess", "/clear"), ("sess", "improve the sky")]
    view = client.get("/api/flights/" + f["id"]).json()["flight"]
    assert view["step_states"]["s2"]["detail"]["receipt"] == "looped 2 times"
    assert any(h["event"] == "looped" for h in view["history"])


def test_moved_steps_keep_their_loops_target():
    """Stops moved onto another line take new ids (a stops-only line dragged
    onto a session, a saved stop tacked on): a Loop among them that repeats
    from one of them follows it to its new id; any other back_to stays."""
    f = fly(AGENT, AGENT, AGENT)
    moved = [
        {"id": "s1", "kind": "agent", "config": {"prompt": "edit"}},
        {"id": "s2", "kind": "command", "config": {"command": "npm run shots"}},
        {"id": "s3", "kind": "loop", "config": {"back_to": "s2", "times": 2}},
    ]
    F.op_add_steps(f, moved, group={"name": "Iterate"}, now=NOW)
    new = f["steps"][3:]
    assert [s["id"] for s in new] == ["s4", "s5", "s6"]
    assert new[2]["config"]["back_to"] == "s5"
    assert F.loop_target(f, new[2]) == 4
    # A back_to outside the batch is left alone (a duplicated Loop on its own line).
    F.op_add_steps(
        f, [{"id": "s9", "kind": "loop", "config": {"back_to": "s1"}}], now=NOW
    )
    assert f["steps"][-1]["config"]["back_to"] == "s1"


def test_a_saved_stop_keeps_its_loops_target():
    plan = F.save_plan(
        {
            "name": "Polish",
            "steps": [
                {"kind": "agent", "config": {"prompt": "edit"}},
                {"kind": "command", "config": {"command": "shots"}},
                {"kind": "loop", "config": {"back_to": "s2", "times": 3}},
            ],
        }
    )
    f = fly(AGENT, AGENT)
    F.op_add_plan(f, plan, now=NOW)
    assert f["steps"][-1]["config"]["back_to"] == f["steps"][3]["id"]


def test_waits_and_loops_take_seconds_and_hours():
    """The owner: loops and waits in seconds or hours, not only minutes."""
    w = F.normalize_steps([{"kind": "wait", "config": {"seconds": 45}}])[0]
    assert w["config"]["seconds"] == 45 and w["title"] == "Wait 45 s"
    w = F.normalize_steps([{"kind": "wait", "config": {"seconds": 7200}}])[0]
    assert w["title"] == "Wait 2 h"
    old = F.normalize_steps([{"kind": "wait", "config": {"minutes": 5}}])[0]
    assert old["config"]["seconds"] == 300
    lp = F.normalize_steps(
        [{"kind": "loop", "config": {"times": 2, "every_seconds": 90}}]
    )[0]
    assert (
        lp["config"]["every_seconds"] == 90
        and lp["title"] == "Loop ×2 every 1 min 30 s"
    )
    assert F.duration_word(30) == "30 s" and F.duration_word(5400) == "1 h 30 min"
