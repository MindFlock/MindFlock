"""The plug-in kind registry (``backend.web.core.flight_kinds``, docs/flights.md
"Round 15"): a registered kind shows up in the kinds dto, is normalised by
its own config function, judged by its own judge, run by its own runner,
observed into ``obs[kind]`` and reached by ``/steps/{sid}/op/{name}``."""

import asyncio
import time

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flight_kinds as K
from backend.web.core import flights as F

NOW = time.time()


def _cfg(raw, strict, where):
    n = F._int(raw.get("n"), 1, 1, 9)
    if strict and raw.get("n") == 99:
        raise F.PlanError("n is 1..9", where + ".n")
    return {"n": n}


def _judge(flight, step, ss, obs, now):
    seen = obs.get("echo") or {}
    if seen.get("ready"):
        return {
            "action": "done",
            "note": "echoed",
            "detail": {"receipt": "n=%d" % step["config"]["n"]},
        }
    return {"action": "wait", "note": "echoing"}


async def _run(flight, step, action, now):
    return dict(action, detail={"started": True})


async def _observe(flight, step, ss, sess, now):
    return {"ready": ss["detail"].get("started") is True}


def _bump(flight, step, body):
    F.mutate(
        flight["id"],
        lambda f: f["step_states"][step["id"]]["detail"].update(
            bumped=body.get("by", 1)
        ),
    )
    return {"ok": True}


@pytest.fixture
def echo():
    K.register(
        K.Kind(
            kind="echo",
            spec={
                "label": "Echo",
                "summary": "A test kind.",
                "default_title": "Echo",
                "risk": ["remote"],
                "default_on_fail": "you",
                "fields": [F._f("n", "int", "N", default=1)],
            },
            config=_cfg,
            judge=_judge,
            run=_run,
            observe=_observe,
            needs_session=False,
            default_title=lambda c: "Echo x%d" % c["n"],
            ops={"bump": _bump},
        )
    )
    F.install_kinds()
    yield
    K._REGISTRY.pop("echo", None)
    F.STEP_KINDS.pop("echo", None)
    F.install_kinds()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    yield


def test_registered_kind_is_listed_with_its_risk(echo):
    dto = F.kinds_dto()
    row = next(k for k in dto["kinds"] if k["kind"] == "echo")
    assert row["label"] == "Echo"
    assert row["risk"] == ["remote"]
    assert row["fields"][0]["name"] == "n"
    assert "echo" not in F.SESSION_KINDS
    assert F.kinds_dto()["kinds"][0]["kind"] == "agent", "built-ins stay first"


def test_builtin_risk_is_served():
    rows = {k["kind"]: k for k in F.kinds_dto()["kinds"]}
    assert rows["ship"]["risk"] == ["remote"]
    assert rows["agent"]["risk"] == []
    assert rows["agent"]["family"] == "work" and rows["you"]["family"] == "people"
    assert rows["ship"]["default_on_fail"] == "halt"


def test_registered_kind_normalises_and_titles_itself(echo):
    step = F.normalize_step({"kind": "echo", "config": {"n": "3"}}, 0)
    assert step["config"] == {"n": 3}
    assert step["title"] == "Echo x3"
    assert step["on_fail"] == "you", "the kind's default_on_fail"
    assert F.normalize_step({"kind": "echo", "on_fail": "skip"}, 0)["on_fail"] == "skip"
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "echo", "config": {"n": 99}}, 0)
    assert err.value.field == "steps[0].config.n"


def test_unknown_kind_is_still_refused(echo):
    with pytest.raises(F.PlanError):
        F.normalize_step({"kind": "nosuch"}, 0)


def test_registered_kind_is_judged_and_run(echo):
    plan = F.adhoc_plan([{"kind": "echo", "config": {"n": 2}}], name="t")
    f = F.take_off(plan, session="s", now=NOW)
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    assert F.next_action(f, {"session": {"present": True}}, NOW)["action"] == "run"
    ss["state"] = "running"
    assert F.judge_for("echo") is _judge
    out = F.next_action(
        f, {"session": {"present": True}, "echo": {"ready": False}}, NOW
    )
    assert out["action"] == "wait" and out["note"] == "echoing"
    out = F.next_action(f, {"session": {"present": True}, "echo": {"ready": True}}, NOW)
    assert out["action"] == "done" and out["detail"]["receipt"] == "n=2"
    ran = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert ran["detail"] == {"started": True}


def test_registered_kind_is_observed(echo, monkeypatch):
    monkeypatch.setattr(D, "_session_obs", lambda flight, now, agent: {"present": True})
    plan = F.adhoc_plan([{"kind": "echo", "config": {"n": 2}}], name="t")
    f = F.take_off(plan, session="s", now=NOW)
    ss = f["step_states"][f["steps"][0]["id"]]
    ss["state"] = "running"
    ss["detail"]["started"] = True
    obs = asyncio.run(D.observe(f, NOW))
    assert obs["echo"] == {"ready": True}


def test_step_op_reaches_the_kind(echo, monkeypatch):
    monkeypatch.setattr(D, "_announce", lambda before, after: None)
    monkeypatch.setattr(D, "wake", lambda: None)
    plan = F.adhoc_plan([{"kind": "echo", "config": {"n": 2}}], name="t")
    f = F.take_off(plan, session="s", now=NOW)
    sid = f["steps"][0]["id"]
    assert D.step_op(f["id"], sid, "bump", {"by": 3}) == {"ok": True}
    assert F.get(f["id"])["step_states"][sid]["detail"]["bumped"] == 3
    with pytest.raises(F.FlightError) as err:
        D.step_op(f["id"], sid, "nosuch", {})
    assert err.value.error == "unknown_op"


def test_step_failed_is_the_drivers(echo):
    assert D._StepFailed is K.StepFailed
