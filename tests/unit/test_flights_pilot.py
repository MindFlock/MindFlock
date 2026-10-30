"""The agent stop's "switch pilot" fields (docs/flights.md "Round 15", task
``review-pilot``): ``program`` + ``model`` are normalised with the agent's
config and, on a ``fresh`` stop, become the new session's CLI and model over
the plan's own."""

import asyncio

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    yield


def test_the_agent_kind_lists_program_and_model():
    kinds = {k["kind"]: k for k in F.kinds_dto()["kinds"]}
    fields = {f["name"]: f for f in kinds["agent"]["fields"]}
    assert fields["program"]["type"] == "text" and fields["program"]["default"] == ""
    assert "fresh" in fields["program"]["label"].lower()
    assert fields["model"]["type"] == "text" and fields["model"]["default"] == ""


def test_agent_config_normalises_program_and_model():
    s = F.normalize_step({"kind": "agent", "config": {"prompt": "x"}})
    assert s["config"]["program"] == "" and s["config"]["model"] == ""
    s = F.normalize_step(
        {
            "kind": "agent",
            "config": {
                "fresh": True,
                "program": "  codex ",
                "model": "m" * 300,
            },
        }
    )
    assert s["config"]["program"] == "codex"
    assert s["config"]["model"] == "m" * 200
    s = F.normalize_step({"kind": "agent", "config": {"program": "p" * 150}})
    assert s["config"]["program"] == "p" * 100


def _fresh_flight(**agent):
    plan = F.save_plan(
        {
            "name": "Pilot",
            "session": {"repo_path": "/repo", "program": "claude", "model": "opus"},
            "steps": [
                {
                    "kind": "agent",
                    "config": dict({"prompt": "go", "fresh": True}, **agent),
                }
            ],
        },
        now=NOW,
    )
    return F.take_off(plan, session="s", now=NOW)


def _run(monkeypatch, f):
    made: list = []

    async def _create(payload):
        made.append(payload)
        return "fresh-1"

    monkeypatch.setattr(D, "_create_session", _create)
    out = asyncio.run(D._run_agent(f, f["steps"][0], {"action": "run"}, NOW))
    return out, made


def test_a_fresh_stop_switches_pilot(monkeypatch):
    f = _fresh_flight(program="codex", model="gpt-5.5")
    out, made = _run(monkeypatch, f)
    assert out["session"] == "fresh-1" and out["detail"]["fresh"] is True
    assert made == [
        {
            "repo_path": "/repo",
            "prompt": "go",
            "program": "codex",
            "profile_model": "gpt-5.5",
        }
    ]


def test_a_fresh_stop_without_them_keeps_the_plans_pilot(monkeypatch):
    f = _fresh_flight()
    _out, made = _run(monkeypatch, f)
    assert made[0]["program"] == "claude" and made[0]["profile_model"] == "opus"


def test_program_and_model_do_nothing_without_fresh(monkeypatch):
    f = _fresh_flight(fresh=False, program="codex", model="gpt-5.5")
    sent: list = []
    made: list = []

    async def _create(payload):
        made.append(payload)
        return "nope"

    monkeypatch.setattr(D, "_create_session", _create)
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append((title, text)))
    out = asyncio.run(D._run_agent(f, f["steps"][0], {"action": "run"}, NOW))
    assert made == [] and sent == [("s", "go")]
    assert out["detail"] == {"sent": True} and "session" not in out
