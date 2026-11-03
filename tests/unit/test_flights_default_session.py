"""The default folder (2026-11-02): a chain run with no session picked runs in
a folder — MindFlock's own ~/.mindflock/default unless general.default_folder
names one. A chain that needs no session (perches, waits, notifies,
commands, Loops) runs FREE: nothing is started for it, like an n8n workflow;
when a run ends it goes back on the board, ready to run again."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.config.settings import update_settings
from backend.web.core import flight_driver as D
from backend.web.core import flights as F


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


def _srv(monkeypatch, instances=None):
    ns = SimpleNamespace(ENGINE=SimpleNamespace(instances=instances or {}))
    monkeypatch.setattr(D, "_server", lambda: ns)
    return ns


def step(kind, **cfg):
    return {"kind": kind, "config": cfg}


def test_needs_agent_only_for_stops_that_type_to_the_agent():
    assert not D.needs_agent({"steps": [step("you"), step("wait")]})
    assert D.needs_agent({"steps": [step("agent")]})
    assert D.needs_agent({"steps": [step("verify", who="both")]})
    assert not D.needs_agent({"steps": [step("verify", who="you")]})
    # A Loop first on the chain repeats the session's prompt: it needs the agent.
    assert D.needs_agent({"steps": [step("loop"), step("wait")]})


def test_needs_session_only_for_agents_and_session_work():
    assert not D.needs_session({"steps": [step("you"), step("wait"), step("notify")]})
    assert not D.needs_session({"steps": [step("command", command="ls")]})
    assert not D.needs_session(
        {"steps": [step("you"), step("wait"), step("loop", back_to="x")]}
    )
    assert D.needs_session({"steps": [step("ship")]})
    assert D.needs_session({"steps": [step("agent")]})


def test_the_default_folder_is_mindflocks_own_place_made_as_a_git_repo(
    monkeypatch, tmp_path
):
    folder = D._default_folder()
    assert folder == str(tmp_path / ".mindflock" / "default")
    import os

    assert os.path.isdir(os.path.join(folder, ".git"))
    assert os.path.exists(os.path.join(folder, "README.md"))
    # A set default_folder that exists wins.
    other = tmp_path / "proj"
    other.mkdir()
    update_settings(general={"default_folder": str(other)})
    assert D._default_folder() == str(other)


def _grounded(steps):
    return F.ground(raw_steps=steps)


def _no_session_made(monkeypatch):
    async def boom(payload):  # pragma: no cover — must not run
        raise AssertionError("a session was made: %r" % payload)

    monkeypatch.setattr(D, "_create_session", boom)


def test_a_chain_with_no_session_work_runs_free_in_the_default_folder(
    monkeypatch, tmp_path
):
    _srv(monkeypatch)
    _no_session_made(monkeypatch)
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.setattr(D, "_emit", lambda *a, **k: None)
    g = _grounded([{"kind": "you", "config": {"text": "check"}}, {"kind": "wait"}])
    f = asyncio.run(D.attach(g["id"], {"title": D.DEFAULT_SESSION}))
    assert f["session"] == ""
    assert f["free"] is True
    assert f["state"] in F.ACTIVE
    assert f["repo_root"] == str(tmp_path / ".mindflock" / "default")


def test_attach_folder_runs_free_there(monkeypatch, tmp_path):
    _srv(monkeypatch)
    _no_session_made(monkeypatch)
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.setattr(D, "_emit", lambda *a, **k: None)
    there = tmp_path / "proj"
    there.mkdir()
    g = _grounded([{"kind": "command", "config": {"command": "true"}}])
    f = asyncio.run(D.attach(g["id"], {"folder": str(there)}))
    assert (f["session"], f["repo_root"], f["free"]) == ("", str(there), True)
    # The command runs in the folder: the session facts carry it.
    assert D._session_obs(f, 0.0, False)["folder"] == str(there)
    assert F._session_hold(f, f["steps"][0], {"present": False}) is None


def test_a_chain_that_needs_a_session_gets_a_new_one_in_the_folder(monkeypatch):
    srv = _srv(monkeypatch)
    made = []

    async def make(payload):
        made.append(payload)
        srv.ENGINE.instances["s1"] = SimpleNamespace(Branch="b")
        return "s1"

    monkeypatch.setattr(D, "_create_session", make)
    monkeypatch.setattr(D, "_repo_root", lambda t: "")
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.setattr(D, "_emit", lambda *a, **k: None)
    g = _grounded([{"kind": "agent", "config": {"prompt": "hi"}}])
    f = asyncio.run(D.attach(g["id"], {"title": D.DEFAULT_SESSION}))
    assert f["session"] == "s1" and not f.get("free")
    assert made[0]["repo_path"].endswith("/.mindflock/default")


def test_a_free_run_that_ends_goes_back_on_the_board_and_runs_again():
    g = _grounded([{"kind": "you", "config": {"text": "a"}}, {"kind": "wait"}])
    f = F.attach(g["id"], "", "/tmp", "", False)
    assert f["free"]
    f = F.mutate(f["id"], lambda x: F.op_cancel(x, "stopped", 10.0))
    assert f["state"] == F.GROUNDED
    assert f["last_run"] == {"state": "cancelled", "reason": "stopped", "at": 10.0}
    # Run again: from the top, every stop pending.
    F.mutate(
        f["id"],
        lambda x: x["step_states"][x["steps"][0]["id"]].update(state="done"),
    )
    f = F.attach(f["id"], "", "/tmp", "", False)
    assert f["state"] in F.ACTIVE and "last_run" not in f
    assert f["cursor"] == 0
    assert all(s["state"] == "pending" for s in f["step_states"].values())


def test_removing_a_free_chains_last_stop_ends_it_for_good():
    g = _grounded([{"kind": "you", "config": {"text": "a"}}])
    f = F.attach(g["id"], "", "/tmp", "", False)
    f = F.mutate(f["id"], lambda x: F.op_remove_step(x, x["steps"][0]["id"], 5.0))
    assert f["state"] == "cancelled"
