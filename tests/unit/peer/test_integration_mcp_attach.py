"""Peer-mode MCP attach: the sandboxed agent gets ONLY the peer toolset and its
share's socket + token — never the HTTP API's host/port, the auth token, a
session title or the settings file."""

from __future__ import annotations

import json
import os
import stat
import tomllib

import pytest

from backend import providers
from backend.providers import mcp_attach

from ._integration_helpers import SHARE_ID, make_share

TOKEN = "peer-token-" + "q" * 30
AUTH_TOKEN = "host-auth-token-SECRET"
PORT = "48765"


@pytest.fixture
def leaky_env(monkeypatch, tmp_path):
    """Everything a careless attach could copy from the server's env."""
    settings = str(tmp_path / "mindflock" / "settings.json")
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("MINDFLOCK_SERVER_PORT", PORT)
    monkeypatch.setenv("UVICORN_PORT", PORT)
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", settings)
    return settings


def _assert_no_leak(blob: str, settings_path: str) -> None:
    for bad in (
        "MINDFLOCK_AUTH_TOKEN",
        AUTH_TOKEN,
        settings_path,
        "MINDFLOCK_SETTINGS_FILE",
        PORT,
        "MINDFLOCK_PORT",
        "MINDFLOCK_HOST",
        "MINDFLOCK_SESSION_TITLE",
        "MINDFLOCK_MCP_MANAGED",
        "127.0.0.1",
        "TMUX",
    ):
        assert bad not in blob, bad


def test_claude_peer_attach_writes_run_mcp_json(leaky_env):
    p = make_share()
    args = mcp_attach.peer_attach_args(
        providers.resolve("claude"), share_id=SHARE_ID, token=TOKEN
    )
    path = os.path.join(p["run"], "mcp.json")
    assert args[0] == "--mcp-config=" + path
    assert "--strict-mcp-config" in args
    allowed = [a for a in args if a.startswith("--allowedTools=")][0]
    names = allowed.split("=", 1)[1].split(",")
    assert names == ["mcp__mindflock__" + n for n in mcp_attach.peer_tool_names()]
    assert "mcp__mindflock__peer_send" in names
    assert "mcp__mindflock__send_message" not in names
    assert "mcp__mindflock__spawn_session" not in names

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    raw = open(path).read()
    doc = json.loads(raw)
    server = doc["mcpServers"]["mindflock"]
    assert server["args"] == ["-P", "-m", "backend.mcp"]
    assert set(server["env"]) == {
        "MINDFLOCK_MCP_MODE",
        "MINDFLOCK_PEER_SOCKET",
        "MINDFLOCK_PEER_TOKEN",
        "PYTHONPATH",
    }
    assert server["env"]["MINDFLOCK_MCP_MODE"] == "peer"
    assert server["env"]["MINDFLOCK_PEER_SOCKET"] == os.path.join(
        p["run"], "agent.sock"
    )
    assert server["env"]["MINDFLOCK_PEER_TOKEN"] == TOKEN
    _assert_no_leak(raw, leaky_env)
    _assert_no_leak(" ".join(args), leaky_env)


def test_codex_peer_attach_inline_table(leaky_env):
    make_share()
    args = mcp_attach.peer_attach_args(
        providers.resolve("codex"), share_id=SHARE_ID, token=TOKEN
    )
    assert args[0] == "-c" and len(args) == 2
    key, table = args[1].split("=", 1)
    assert key == "mcp_servers.mindflock"
    parsed = tomllib.loads("x = " + table)["x"]
    assert parsed["env"]["MINDFLOCK_MCP_MODE"] == "peer"
    assert "env_vars" not in parsed  # nothing forwarded from the host env
    assert set(parsed["tools"]) == set(mcp_attach.peer_tool_names())
    assert all(t["approval_mode"] == "approve" for t in parsed["tools"].values())
    _assert_no_leak(" ".join(args), leaky_env)


def test_peer_attach_refuses_other_providers_and_missing_token():
    make_share()
    with pytest.raises(ValueError):
        mcp_attach.peer_attach_args(
            providers.resolve("aider"), share_id=SHARE_ID, token=TOKEN
        )
    with pytest.raises(ValueError):
        mcp_attach.peer_attach_args(
            providers.resolve("claude"), share_id=SHARE_ID, token=""
        )
    with pytest.raises(ValueError):
        mcp_attach.peer_attach_args(
            providers.resolve("claude"), share_id="../../etc", token=TOKEN
        )


def test_peer_attach_ignores_the_agent_mcp_kill_switch(monkeypatch):
    # The peer tools ARE the shared session; the general toggle is for the
    # flock MCP. (Off would leave a sandboxed agent with no way to talk.)
    make_share()
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    args = mcp_attach.peer_attach_args(
        providers.resolve("claude"), share_id=SHARE_ID, token=TOKEN
    )
    assert args and args[0].startswith("--mcp-config=")
