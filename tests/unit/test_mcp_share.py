"""Shared MCP servers, launch side (:mod:`backend.providers.mcp_share`,
:mod:`backend.providers.claude_mcp`) and its setting / route / uninstall.

The Claude Code facts these rely on — a same-named ``--mcp-config`` entry
replaces the user's (user, local AND ``.mcp.json`` scope) stdio entry so its
process never starts; per-project ``disabledMcpServers`` / ``deniedMcpServers``
still apply to it; Claude re-initializes on its own when the bridge restarts at
the same URL — were verified against claude 2.1.299 with a scratch HOME and a
stub API (docs/mcp.md, "Shared MCP servers").
"""

from __future__ import annotations

import json
import os
import time
import subprocess
import sys

import pytest

from backend.config import settings as S
from backend.providers import claude_mcp, mcp_attach, mcp_share
from backend.providers.claude import ClaudeProvider

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX bridge")


def _write_settings(general: dict) -> None:
    path = S.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"general": general}))
    S.invalidate()


def _write_claude(cfg: dict) -> None:
    with open(os.environ["MINDFLOCK_CLAUDE_JSON"], "w") as fh:
        json.dump(cfg, fh)


USER = {
    "context7": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "env": {},
    },
    "gitnexus": {"type": "stdio", "command": "gitnexus", "args": ["mcp"]},
    "remote": {"type": "http", "url": "https://example.invalid/mcp"},
    "needsvar": {"command": "x", "env": {"TOKEN": "${MF_TEST_UNSET_VAR}"}},
    "mindflock": {"command": "python", "args": ["-m", "backend.mcp"]},
}


# --------------------------------------------------------------------------- #
# The setting
# --------------------------------------------------------------------------- #
def test_setting_round_trips_and_drops_reserved_and_bad_names():
    g = S.GeneralSettings.from_dict(
        {
            "mcp_shared_servers": [
                "context7",
                "mindflock",
                "bad name",
                "context7",
                "aws-mcp",
            ]
        }
    )
    assert g.mcp_shared_servers == ["context7", "aws-mcp"]
    assert g.to_dict()["mcp_shared_servers"] == ["context7", "aws-mcp"]
    assert "mcp_shared_servers" not in S.GeneralSettings().to_dict()  # default: nothing


def test_setting_is_per_device_never_synced():
    from backend.web.core import settings_sync

    assert "mcp_shared_servers" in settings_sync.LOCAL["general"]
    assert "mcp_shared_servers" not in settings_sync.SYNCED["general"]


def test_shared_names_reads_fresh_and_honours_the_kill_switch(monkeypatch):
    _write_settings({"mcp_shared_servers": ["context7"]})
    assert mcp_share.shared_names() == ["context7"]
    _write_settings({"mcp_shared_servers": ["context7", "gitnexus"]})
    assert mcp_share.shared_names() == ["context7", "gitnexus"]  # no stale cache
    monkeypatch.setenv("MINDFLOCK_MCP_SHARE", "0")
    assert mcp_share.shared_names() == []


# --------------------------------------------------------------------------- #
# Definitions
# --------------------------------------------------------------------------- #
def test_expand_definition_handles_claude_variable_syntax():
    env = {"HOME_X": "/h"}
    d = {
        "command": "${HOME_X}/bin/s",
        "args": ["--a=${MISSING:-dflt}"],
        "env": {"K": "${HOME_X}"},
    }
    assert mcp_share.expand_definition(d, env) == {
        "command": "/h/bin/s",
        "args": ["--a=dflt"],
        "env": {"K": "/h"},
    }
    # A variable only the SESSION might have: not shareable.
    assert (
        mcp_share.expand_definition({"command": "x", "args": ["${NOPE}"]}, env) is None
    )
    # Remote entries have nothing to share; malformed ones are refused.
    assert mcp_share.expand_definition({"type": "http", "url": "u"}, env) is None
    assert (
        mcp_share.expand_definition({"command": "x", "args": "notalist"}, env) is None
    )


def test_share_caution_flags_cwd_and_stateful_servers():
    assert "folder" in mcp_share.share_caution("gitnexus", USER["gitnexus"])
    assert "state" in mcp_share.share_caution(
        "postgres", {"command": "uvx", "args": ["postgres-mcp"]}
    )
    assert mcp_share.share_caution("context7", USER["context7"]) == ""


def test_list_servers_never_exposes_env_and_explains_unshareable_rows():
    _write_claude(
        {"mcpServers": dict(USER, secret={"command": "s", "env": {"TOKEN": "hunter2"}})}
    )
    rows = {r["name"]: r for r in claude_mcp.list_servers()}
    assert "hunter2" not in json.dumps(rows)
    assert rows["context7"]["shareable"] and rows["context7"]["command"] == "npx"
    assert rows["gitnexus"]["shareable"] and rows["gitnexus"]["caution"]
    assert (
        not rows["remote"]["shareable"] and "remote" in rows["remote"]["reason"].lower()
    )
    assert not rows["needsvar"]["shareable"]
    assert not rows["mindflock"]["shareable"]


def test_project_redefinitions_keep_a_folders_own_server(tmp_path):
    proj = tmp_path / "proj"
    sub = proj / "sub"
    sub.mkdir(parents=True)
    cfg = {
        "mcpServers": USER,
        "projects": {
            str(proj): {
                "mcpServers": {
                    "gitnexus": {
                        "command": "npx",
                        "args": ["gitnexus@latest", "mcp"],
                    },  # different
                    "context7": dict(USER["context7"]),  # identical: still shareable
                }
            },
        },
    }
    (sub / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"remote": {"command": "y"}}})
    )
    found = claude_mcp.project_redefinitions(
        str(sub), ["gitnexus", "context7", "remote"], cfg
    )
    assert found == {"gitnexus", "remote"}
    assert claude_mcp.project_redefinitions(str(tmp_path), ["gitnexus"], cfg) == set()


# --------------------------------------------------------------------------- #
# The run file
# --------------------------------------------------------------------------- #
BRIDGE = {"pid": 1, "port": 45678, "token": "tkn"}


def _spec(tmp_path, **kw):
    kw.setdefault("workdir", str(tmp_path))
    return mcp_attach.McpSpec(title="t", tmux_name="mindflock_t", **kw)


def test_shared_entries_point_at_the_bridge(monkeypatch, tmp_path):
    _write_claude(
        {"mcpServers": dict(USER, context7=dict(USER["context7"], timeout=90000))}
    )
    _write_settings(
        {"mcp_shared_servers": ["context7", "gitnexus", "remote", "needsvar", "absent"]}
    )
    published = {}
    monkeypatch.setattr(mcp_share, "ensure_bridge", lambda: BRIDGE)
    monkeypatch.setattr(mcp_share, "publish", lambda defs: published.update(defs))
    entries = claude_mcp.shared_mcp_entries(str(tmp_path))
    assert set(entries) == {"context7", "gitnexus"}
    assert entries["context7"] == {
        "type": "http",
        "url": "http://127.0.0.1:45678/mcp/context7",
        "headers": {"Authorization": "Bearer tkn"},
        "timeout": 90000,
    }
    assert published["context7"] == {
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "env": {},
    }


def test_no_bridge_means_no_override(monkeypatch, tmp_path):
    _write_claude({"mcpServers": USER})
    _write_settings({"mcp_shared_servers": ["context7"]})
    monkeypatch.setattr(mcp_share, "ensure_bridge", lambda: None)
    assert claude_mcp.shared_mcp_entries(str(tmp_path)) == {}


def test_nothing_shared_never_touches_the_bridge(monkeypatch, tmp_path):
    _write_claude({"mcpServers": USER})
    monkeypatch.setattr(
        mcp_share, "ensure_bridge", lambda: pytest.fail("started a bridge")
    )
    assert claude_mcp.shared_mcp_entries(str(tmp_path)) == {}


def test_claude_run_file_carries_shared_entries_beside_mindflock(monkeypatch, tmp_path):
    _write_claude({"mcpServers": USER})
    _write_settings({"mcp_shared_servers": ["context7"]})
    monkeypatch.setattr(mcp_share, "ensure_bridge", lambda: BRIDGE)
    monkeypatch.setattr(mcp_share, "publish", lambda defs: None)
    args = ClaudeProvider().mcp_launch_args(_spec(tmp_path))
    path = args[0].split("=", 1)[1]
    servers = json.load(open(path))["mcpServers"]
    assert set(servers) == {"mindflock", "context7"}
    assert servers["context7"]["type"] == "http"
    assert servers["mindflock"]["type"] == "stdio"
    # No --strict-mcp-config: the user's other servers keep loading.
    assert "--strict-mcp-config" not in args


def test_shared_entries_can_never_displace_mindflocks_own(tmp_path):
    cfg = mcp_attach.claude_config(
        _spec(tmp_path), {"mindflock": {"type": "http", "url": "x"}}
    )
    assert cfg["mcpServers"]["mindflock"]["type"] == "stdio"


def test_a_sharing_failure_never_fails_the_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_mcp, "shared_mcp_entries", lambda wd: 1 / 0)
    args = ClaudeProvider().mcp_launch_args(_spec(tmp_path))
    servers = json.load(open(args[0].split("=", 1)[1]))["mcpServers"]
    assert set(servers) == {"mindflock"}


# --------------------------------------------------------------------------- #
# The real bridge process: start, reuse, same port after a restart, stop
# --------------------------------------------------------------------------- #
def _bridge_log() -> str:
    """The bridge's own log (its stdout/stderr), for a failure message: the
    one place a start that never came up says why."""
    path = os.path.join(mcp_share.state_dir(), "logs", "bridge.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()[-4000:]
    except OSError as err:
        return "(no bridge log: %s)" % err


def test_bridge_lifecycle(tmp_path):
    first = mcp_share.ensure_bridge()
    try:
        assert (
            first and first["port"] and first["token"]
        ), "the bridge never came up; bridge.json=%r\nlog:\n%s" % (
            mcp_share._read_json(os.path.join(mcp_share.state_dir(), "bridge.json")),
            _bridge_log(),
        )
        assert mcp_share.ensure_bridge()["pid"] == first["pid"]  # reused, not restarted
        state = mcp_share.state_dir()
        assert oct(os.stat(state).st_mode & 0o777) == "0o700"
        assert (
            oct(os.stat(os.path.join(state, "bridge.json")).st_mode & 0o777) == "0o600"
        )
        assert mcp_share.bridge_status()["running"]
        # Stops promptly: the bridge is this process's child, so it must be
        # reaped while waiting — a zombie still answers kill(pid, 0), which
        # ran the full timeout and then (on macOS) failed the SIGKILL.
        t0 = time.monotonic()
        assert mcp_share.stop_bridge()
        assert time.monotonic() - t0 < 3.0
        assert mcp_share.bridge_pid() is None
        # A restarted bridge answers at the SAME url with the SAME token, so
        # the run files of windows already open stay valid.
        second = mcp_share.ensure_bridge()
        assert second["pid"] != first["pid"]
        assert (second["port"], second["token"]) == (first["port"], first["token"])
    finally:
        mcp_share.stop_bridge()
    assert not mcp_share.bridge_status()["running"]


def test_bridge_refuses_to_start_without_a_token(tmp_path):
    from backend.mcp import bridge

    env = {k: v for k, v in os.environ.items() if k != bridge.TOKEN_ENV}
    env["PYTHONPATH"] = mcp_attach.mcp_pythonpath()
    res = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "backend.mcp.bridge",
            "--state-dir",
            str(tmp_path / "s"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 2 and "no token" in res.stderr


def test_uninstall_stops_the_bridge(monkeypatch):
    from backend import uninstall

    stopped = []
    monkeypatch.setattr(mcp_share, "stop_bridge", lambda: stopped.append(1) or True)
    report = uninstall.execute(uninstall.Plan(), purge=False, dry_run=False)
    assert stopped and any("shared-MCP bridge" in line for line in report.actions)


# --------------------------------------------------------------------------- #
# The Settings route
# --------------------------------------------------------------------------- #
def test_settings_route_lists_servers_and_shared_state(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    _write_claude({"mcpServers": USER})
    _write_settings({"mcp_shared_servers": ["context7"]})
    client = TestClient(server.app)
    data = client.get("/api/settings/mcp-shared").json()
    rows = {r["name"]: r for r in data["servers"]}
    assert rows["context7"]["shared"] and rows["context7"]["provider"] == "claude"
    assert not rows["gitnexus"]["shared"]
    assert data["shared"] == ["context7"]
    assert data["bridge"]["running"] is False
    # Saving rides the normal settings POST (owner-only via may_configure).
    r = client.post(
        "/api/settings", json={"general": {"mcp_shared_servers": ["gitnexus"]}}
    )
    assert r.status_code == 200
    assert S.load_settings().general.mcp_shared_servers == ["gitnexus"]
