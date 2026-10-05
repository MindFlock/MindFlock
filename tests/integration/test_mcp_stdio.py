"""End to end: ``python -P -m backend.mcp`` as a real subprocess, speaking MCP
over real stdio to this test, and HTTP to a tiny stdlib fake of the MindFlock
API.

The subprocess runs from a cwd holding a DECOY ``backend/`` package that
raises on import — the situation of an agent working inside a MindFlock
checkout. It only starts because ``-P`` keeps the cwd off ``sys.path`` and
``PYTHONPATH`` points at the real package. The fake API also demands the
bearer token, proving the client sends it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOKEN = "e2e-token-123"


class _FakeMindFlock(BaseHTTPRequestHandler):
    rows = [
        {
            "title": "orch",
            "status": "running",
            "activity": "working",
            "activity_since": 1.0,
            "program": "claude",
            "provider": "claude",
            "branch": "mindflock/orch",
            "folder": "/nonexistent/orch",
            "path": "/nonexistent/repo",
            "repo": "repo",
            "tmux_name": "mindflock_orch",
            "parent": "",
            "spawned": False,
        },
        {
            "title": "w1",
            "status": "running",
            "activity": "idle",
            "activity_since": 1.0,
            "program": "claude",
            "provider": "claude",
            "branch": "mindflock/w1",
            "folder": "/nonexistent/w1",
            "path": "/nonexistent/repo",
            "repo": "repo",
            "tmux_name": "mindflock_w1",
            "parent": "orch",
            "spawned": True,
        },
    ]
    posted: list = []
    unauthorized: list = []

    def log_message(self, *a):  # keep test output quiet
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if self.headers.get("Authorization") == "Bearer " + _TOKEN:
            return True
        type(self).unauthorized.append(self.path)
        self._send(401, {"error": "unauthorized"})
        return False

    def do_GET(self):
        if not self._authed():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/config":
            self._send(
                200,
                {
                    "default_program": "claude",
                    "caps": {
                        "git": True,
                        "agent_mcp": {
                            "enabled": True,
                            "providers": ["claude", "codex"],
                        },
                    },
                },
            )
        elif path == "/api/instances":
            self._send(200, self.rows)
        elif path.endswith("/messages"):
            self._send(200, {"messages": [], "unread": 0, "version": 0})
        else:
            self._send(404, {"detail": "Not Found"})

    def do_POST(self):
        if not self._authed():
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).posted.append((self.path, payload))
        if self.path.endswith("/messages"):
            self._send(
                201,
                {
                    "message": {"id": "m1_1", "state": "pending", **payload},
                    "delivery": "pending",
                },
            )
        else:
            self._send(404, {"detail": "Not Found"})


@pytest.fixture()
def fake_server():
    _FakeMindFlock.posted = []
    _FakeMindFlock.unauthorized = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMindFlock)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def decoy_cwd(tmp_path):
    decoy = tmp_path / "agent-worktree" / "backend"
    decoy.mkdir(parents=True)
    (decoy / "__init__.py").write_text(
        'raise ImportError("DECOY backend imported: -P did not take effect")\n'
    )
    return decoy.parent


class _Client:
    def __init__(self, proc):
        self.proc = proc
        self.n = 0

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def recv(self):
        line = self.proc.stdout.readline()
        assert line, (
            "server closed stdout; stderr:\n" + self.proc.stderr.read().decode()
        )
        return json.loads(line)

    def request(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        resp = self.recv()
        assert resp["id"] == self.n, resp
        return resp

    def call_tool(self, name, arguments=None):
        resp = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        result = resp["result"]
        assert result["isError"] is False, result
        return json.loads(result["content"][0]["text"])


def _env(port, tmp_path):
    keep = {k: os.environ[k] for k in ("PATH", "LANG", "SYSTEMROOT") if k in os.environ}
    return {
        **keep,
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(_REPO_ROOT),
        "MINDFLOCK_HOST": "127.0.0.1",
        "MINDFLOCK_PORT": str(port),
        "MINDFLOCK_SESSION_TITLE": "orch",
        "MINDFLOCK_MCP_MANAGED": "1",
        "MINDFLOCK_AUTH_TOKEN": _TOKEN,
        "MINDFLOCK_SETTINGS_FILE": str(tmp_path / "settings.json"),
    }


def test_stdio_server_end_to_end(fake_server, decoy_cwd, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-P", "-m", "backend.mcp"],
        cwd=str(decoy_cwd),
        env=_env(fake_server, tmp_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        c = _Client(proc)
        # The 2026-07-28 probe before initialize: an immediate error, same id.
        probe = c.request("server/discover", {})
        assert probe["error"]["code"] == -32601

        init = c.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "0"},
            },
        )["result"]
        assert init["protocolVersion"] == "2025-06-18"
        assert init["serverInfo"]["name"] == "mindflock"
        assert "whoami" in init["instructions"]
        c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = c.request("tools/list")["result"]["tools"]
        assert len(tools) == 14
        assert {t["name"] for t in tools} >= {"whoami", "spawn_session", "kill_session"}

        me = c.call_tool("whoami")
        assert me["session"]["title"] == "orch"
        assert me["children"] == ["w1"]
        assert me["scope"] == "children"

        listed = c.call_tool("list_sessions")
        assert [r["title"] for r in listed["sessions"]] == ["w1"]
        assert listed["sessions"][0]["managed"] is True

        sent = c.call_tool("send_message", {"to": "w1", "text": "please rebase"})
        assert sent["sent"][0]["delivery"] == "pending"
        path, payload = _FakeMindFlock.posted[-1]
        assert path == "/api/instances/w1/messages"
        assert payload == {"text": "please rebase", "from": "orch", "delivery": "auto"}
        # Discovery's fingerprint probe goes out WITHOUT the token (whatever
        # squats the port must never see it); once the gate has answered, every
        # request carries it.
        assert _FakeMindFlock.unauthorized == ["/api/config"]

        # A schema violation is a tool result, not a protocol error.
        bad = c.request(
            "tools/call", {"name": "send_message", "arguments": {"to": "w1"}}
        )
        assert bad["result"]["isError"] is True

        # stdin EOF: the server exits promptly and cleanly.
        proc.stdin.close()
        start = time.monotonic()
        assert proc.wait(timeout=10) == 0
        assert time.monotonic() - start < 10
        assert proc.stdout.read() == b""  # nothing but protocol was ever written
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_mindflock_mcp_cli_is_the_same_server(fake_server, decoy_cwd, tmp_path):
    """`mindflock mcp` (cli.main) serves the identical protocol."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-P",
            "-c",
            "import sys; from backend import cli; sys.exit(cli.main(['mcp']))",
        ],
        cwd=str(decoy_cwd),
        env=_env(fake_server, tmp_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        c = _Client(proc)
        init = c.request(
            "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}
        )
        assert init["result"]["protocolVersion"] == "2024-11-05"
        assert c.request("ping")["result"] == {}
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
