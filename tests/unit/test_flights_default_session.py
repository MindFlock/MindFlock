"""The default session (2026-11-02): a session is a place in the file tree; a
chain run with no session picked runs in the default one — made the first
time (in-place, "default", in the default folder) and remembered."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.config.settings import load_settings, update_settings
from backend.web.core import flight_driver as D


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    yield


def _srv(monkeypatch, instances):
    ns = SimpleNamespace(ENGINE=SimpleNamespace(instances=instances))
    monkeypatch.setattr(D, "_server", lambda: ns)
    return ns


def test_a_remembered_shell_default_session_is_reused_for_a_chain_with_no_agent(
    monkeypatch,
):
    _srv(monkeypatch, {"default": SimpleNamespace(Path="/x", Program="zsh")})
    update_settings(general={"default_session": "default"})
    made = []
    monkeypatch.setattr(D, "_create_session", lambda p: made.append(p))
    assert asyncio.run(D.ensure_default_session()) == "default"
    assert made == []


def test_no_agent_needed_makes_a_plain_shell_session_in_the_default_place(
    monkeypatch, tmp_path
):
    # A remembered "default" that runs claude is not reused for a chain with no agent.
    _srv(monkeypatch, {"default": SimpleNamespace(Path="/elsewhere", Program="claude")})
    update_settings(general={"default_session": "default"})
    made = []

    async def create(payload):
        made.append(payload)
        return "default-2"

    monkeypatch.setattr(D, "_create_session", create)
    assert asyncio.run(D.ensure_default_session(agent=False)) == "default-2"
    folder = str(tmp_path / ".mindflock" / "default")
    assert made == [
        {
            "title": "default",
            "repo_path": folder,
            "prompt": "",
            "in_place": True,
            "program": "zsh",
        }
    ]
    assert load_settings().general.default_session == "default-2"


def test_a_chain_that_needs_an_agent_gets_the_agent_session(monkeypatch, tmp_path):
    _srv(monkeypatch, {})
    made = []

    async def create(payload):
        made.append(payload)
        return "default-agent"

    monkeypatch.setattr(D, "_create_session", create)
    assert asyncio.run(D.ensure_default_session(agent=True)) == "default-agent"
    assert made[0]["title"] == "default-agent" and "program" not in made[0]


def test_needs_agent():
    step = lambda k, **c: {"kind": k, "config": c}  # noqa: E731
    assert not D.needs_agent(
        {"steps": [step("you"), step("wait"), step("loop", back_to="s1")]}
    )
    assert D.needs_agent({"steps": [step("agent")]})
    assert D.needs_agent({"steps": [step("verify", who="both")]})
    assert not D.needs_agent({"steps": [step("verify", who="you")]})
    # A Loop first on the chain repeats the session's prompt: it needs the agent.
    assert D.needs_agent({"steps": [step("loop"), step("wait")]})


def test_the_default_folder_is_mindflocks_own_place_made_as_a_git_repo(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
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


def test_attach_names_it_by_at_default():
    assert D.DEFAULT_SESSION == "@default"
