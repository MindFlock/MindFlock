"""Peer-mode MCP (backend.mcp.peer_tools): the sandboxed agent's toolset.

With ``MINDFLOCK_MCP_MODE=peer`` the server must expose exactly the peer
tools, talk only to the share's AF_UNIX agent API, never attempt HTTP/TCP,
and fail every call when its socket/token env is missing. The ordinary
server must be unchanged when the mode is not set.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from backend.mcp import peer_tools as pt
from backend.mcp.protocol import McpServer, Tool, ToolContext, ToolError
from backend.peer import agent_api as aa
from backend.peer import share as sh
from backend.web.core import mailbox
from tests.unit.peer.test_agent_api import TITLE, TOKEN, FakeService
from tests.unit.peer.test_share import LINK, repo  # noqa: F401 — fixture

ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

ORDINARY_TOOLS = [
    "spawn_session",
    "send_message",
    "list_sessions",
    "get_session",
    "read_output",
    "get_diff",
    "check_inbox",
    "wait_for_message",
    "report_result",
    "kill_session",
    "ship_session",
    "start_team_run",
    "set_autopilot",
    "answer_prompt",
    "spawn_ticket_session",
    "fence_session",
]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
class Harness:
    """An McpServer on a thread, wired to two OS pipes (as on stdio)."""

    def __init__(self, server_factory):
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        self._to = os.fdopen(w_in, "wb", buffering=0)
        self._from = os.fdopen(r_out, "rb")
        self.eof = threading.Event()
        self.server = server_factory(
            stdin=os.fdopen(r_in, "rb"),
            stdout=os.fdopen(w_out, "wb"),
            on_eof=self.eof.set,
        )
        self.lines: "queue.Queue[dict]" = queue.Queue()
        threading.Thread(target=self.server.run, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()
        self._id = 0

    def _read(self):
        for line in self._from:
            self.lines.put(json.loads(line))

    def request(self, method, params=None, timeout=10):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self._to.write(json.dumps(msg).encode() + b"\n")
        while True:
            got = self.lines.get(timeout=timeout)
            if got.get("id") == self._id:
                return got

    def init(self):
        return self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        )

    def call(self, name, args=None, timeout=10):
        return self.request(
            "tools/call", {"name": name, "arguments": args or {}}, timeout=timeout
        )

    def close(self):
        self._to.close()


def peer_harness(env):
    return Harness(lambda **kw: pt.build_peer_server(env=env, **kw))


def text_of(resp):
    return resp["result"]["content"][0]["text"]


class ApiThread:
    """A real AgentApi on its own event loop thread."""

    def __init__(self, share, svc, **kw):
        self.loop = asyncio.new_event_loop()
        self.api = aa.AgentApi(share, LINK, TOKEN, svc, TITLE, **kw)
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.api.start())
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def stop(self):
        fut = asyncio.run_coroutine_threadsafe(self.api.stop(), self.loop)
        fut.result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


@pytest.fixture
def short_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="mfp", dir="/tmp")
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", d)
    yield d
    sh._rmtree(d)


@pytest.fixture
def share(short_home, repo):  # noqa: F811
    return sh.create_share(LINK, str(repo))


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(share, svc):
    t = ApiThread(share, svc)
    yield t
    t.stop()


@pytest.fixture
def env(live):
    return {
        "MINDFLOCK_MCP_MODE": "peer",
        "MINDFLOCK_PEER_SOCKET": live.api.path,
        "MINDFLOCK_PEER_TOKEN": TOKEN,
    }


@pytest.fixture
def no_network(monkeypatch):
    """Fail the test on any attempt at TCP/HTTP."""
    attempts = []

    def deny(name):
        def f(*a, **kw):
            attempts.append(name)
            raise AssertionError("network attempted: %s" % name)

        return f

    import http.client
    import urllib.request

    monkeypatch.setattr(socket, "create_connection", deny("create_connection"))
    monkeypatch.setattr(http.client.HTTPConnection, "connect", deny("http"))
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", deny("https"))
    monkeypatch.setattr(urllib.request, "urlopen", deny("urlopen"))
    real_connect = socket.socket.connect

    def guarded(self, addr):
        if self.family != socket.AF_UNIX:
            attempts.append("socket.connect %r" % (addr,))
            raise AssertionError("non-unix connect: %r" % (addr,))
        return real_connect(self, addr)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    return attempts


# --------------------------------------------------------------------------- #
# The toolset
# --------------------------------------------------------------------------- #
def test_peer_tool_names_constant():
    assert isinstance(pt.PEER_TOOL_NAMES, tuple)
    assert pt.PEER_TOOL_NAMES == (
        "whoami",
        "peer_send",
        "peer_inbox",
        "peer_get_diff",
        "peer_read_file",
        "peer_list_files",
        "checkpoint",
    )


def test_tools_list_is_exactly_the_peer_tools():
    h = peer_harness({"MINDFLOCK_MCP_MODE": "peer"})
    init = h.init()["result"]
    assert init["serverInfo"]["name"] == "mindflock"
    tools = h.request("tools/list")["result"]["tools"]
    assert tuple(t["name"] for t in tools) == pt.PEER_TOOL_NAMES
    for t in tools:
        assert t["inputSchema"]["additionalProperties"] is False
    h.close()


def test_instructions():
    text = pt.PEER_INSTRUCTIONS
    assert 0 < len(text) < 1900
    low = text.lower()
    for needle in (
        "sandbox",
        "shared folder",
        "untrusted",
        "peer_send",
        "checkpoint",
        "read-only",
        "secret",
    ):
        assert needle in low, needle
    h = peer_harness({"MINDFLOCK_MCP_MODE": "peer"})
    assert h.init()["result"]["instructions"] == text
    h.close()


@pytest.mark.parametrize("name", ORDINARY_TOOLS)
def test_ordinary_tools_absent_and_uncallable(name):
    h = peer_harness(
        {
            "MINDFLOCK_MCP_MODE": "peer",
            "MINDFLOCK_PEER_SOCKET": "/x",
            "MINDFLOCK_PEER_TOKEN": TOKEN,
        }
    )
    h.init()
    resp = h.call(name, {"title": "x", "prompt": "rm -rf ~"})
    assert resp["error"]["code"] == -32602
    assert "unknown tool" in resp["error"]["message"]
    h.close()


@pytest.mark.parametrize(
    "env",
    [
        {"MINDFLOCK_MCP_MODE": "peer"},
        {"MINDFLOCK_MCP_MODE": "peer", "MINDFLOCK_PEER_SOCKET": "/tmp/x.sock"},
        {"MINDFLOCK_MCP_MODE": "peer", "MINDFLOCK_PEER_TOKEN": TOKEN},
        {
            "MINDFLOCK_MCP_MODE": "peer",
            "MINDFLOCK_PEER_SOCKET": " ",
            "MINDFLOCK_PEER_TOKEN": " ",
        },
    ],
)
def test_missing_env_every_tool_errors(env, no_network, monkeypatch):
    made = []
    real = socket.socket

    def spy(*a, **kw):
        made.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(pt.socket, "socket", spy)
    h = peer_harness(env)
    h.init()
    args = {"peer_send": {"text": "x"}, "peer_read_file": {"path": "a"}}
    for name in pt.PEER_TOOL_NAMES:
        resp = h.call(name, args.get(name, {}))
        assert resp["result"]["isError"] is True, name
        assert "not configured" in text_of(resp)
    assert made == []  # never even tried to connect
    assert no_network == []
    h.close()


def test_unreachable_socket_errors_cleanly(short_home, no_network):
    h = peer_harness(
        {
            "MINDFLOCK_MCP_MODE": "peer",
            "MINDFLOCK_PEER_SOCKET": os.path.join(short_home, "nope.sock"),
            "MINDFLOCK_PEER_TOKEN": TOKEN,
        }
    )
    h.init()
    resp = h.call("whoami")
    assert resp["result"]["isError"] is True
    assert "cannot reach" in text_of(resp)
    assert no_network == []
    h.close()


# --------------------------------------------------------------------------- #
# Against a real agent API — and no network
# --------------------------------------------------------------------------- #
def test_all_tools_over_unix_socket_only(env, live, share, svc, no_network):
    mailbox.post(
        TITLE,
        "hi from peer",
        sender="peer:Bob",
        data={"peer_msg_id": "pm1", "link_id": LINK},
    )
    h = peer_harness(env)
    h.init()

    who = json.loads(text_of(h.call("whoami")))
    assert who == {
        "share_id": share.share_id,
        "folder": share.work,
        "peer_name": "Bob",
        "connected": True,
    }

    sent = json.loads(text_of(h.call("peer_send", {"text": "yo", "reply_to": "pm1"})))
    assert sent["delivered"] is True
    assert svc.calls[-1][1:3] == (
        "msg",
        {"msg_id": sent["msg_id"], "text": "yo", "reply_to": "pm1"},
    )

    inbox = json.loads(text_of(h.call("peer_inbox", {})))
    assert [m["text"] for m in inbox["messages"]] == ["hi from peer"]
    assert inbox["messages"][0]["untrusted"] is True

    assert (
        json.loads(text_of(h.call("peer_get_diff", {"max_chars": 2000})))
        == svc.responses["diff"]
    )
    assert svc.calls[-1][1:3] == ("diff", {"max_chars": 2000})
    assert (
        json.loads(text_of(h.call("peer_read_file", {"path": "a.txt"})))["content"]
        == "c"
    )
    assert svc.calls[-1][1:3] == ("read_file", {"path": "a.txt"})
    assert json.loads(text_of(h.call("peer_list_files")))["files"] == ["a"]

    open(os.path.join(share.work, "mine.txt"), "w").write("m\n")
    sha = json.loads(text_of(h.call("checkpoint", {"message": "agent commit"})))["sha"]
    assert sha == sh.git(share, "rev-parse", "HEAD").stdout.decode().strip()
    assert no_network == []
    h.close()


def test_peer_errors_surface_as_tool_errors(env, svc):
    svc.connected = False
    h = peer_harness(env)
    h.init()
    resp = h.call("peer_send", {"text": "x"})
    assert resp["result"]["isError"] is True
    assert "not connected" in text_of(resp)
    h.close()


def test_wrong_token_is_unauthorized(env):
    env = dict(env, MINDFLOCK_PEER_TOKEN="wrong-token-wrong-token")
    h = peer_harness(env)
    h.init()
    resp = h.call("whoami")
    assert resp["result"]["isError"] is True
    assert "unauthorized" in text_of(resp)
    h.close()


@pytest.mark.parametrize(
    "name,args",
    [
        ("peer_send", {}),
        ("peer_send", {"text": ""}),
        ("peer_send", {"text": "x" * 20001}),
        ("peer_send", {"text": "x", "to": "orchestrator"}),
        ("peer_inbox", {"wait_s": 5000}),
        ("peer_inbox", {"title": "orchestrator"}),
        ("peer_get_diff", {"max_chars": 10}),
        ("peer_read_file", {}),
        ("whoami", {"session": "x"}),
        ("checkpoint", {"message": "x" * 3000}),
        ("peer_list_files", {"host": "evil.example"}),
    ],
)
def test_schema_rejects_bad_args(env, svc, name, args):
    h = peer_harness(env)
    h.init()
    resp = h.call(name, args)
    assert resp["result"]["isError"] is True
    assert "invalid arguments" in text_of(resp)
    assert svc.calls == []
    h.close()


def test_peer_inbox_waits_in_slices_and_wakes(env, monkeypatch, no_network):
    monkeypatch.setattr(pt, "WAIT_SLICE_S", 1)
    h = peer_harness(env)
    h.init()

    def later():
        time.sleep(1.5)
        mailbox.post(TITLE, "late", sender="peer:Bob")

    threading.Thread(target=later, daemon=True).start()
    t0 = time.monotonic()
    resp = h.call("peer_inbox", {"wait_s": 20}, timeout=30)
    assert [m["text"] for m in json.loads(text_of(resp))["messages"]] == ["late"]
    assert time.monotonic() - t0 < 10
    h.close()


def test_peer_inbox_honours_cancellation():
    calls = []

    class Client:
        configured = True

        def call(self, op, args, timeout=0):
            calls.append(args)
            ctx.cancelled.set()  # cancelled while the first slice ran
            return {"messages": [], "unread": 0}

    tools = {t.name: t for t in pt.build_peer_tools(Client())}
    ctx = ToolContext()
    from backend.mcp.protocol import Cancelled

    with pytest.raises(Cancelled):
        tools["peer_inbox"].handler({"wait_s": 600}, ctx)
    assert len(calls) == 1


def test_peer_inbox_zero_wait_is_one_call():
    calls = []

    class Client:
        def call(self, op, args, timeout=0):
            calls.append((op, args))
            return {"messages": [], "unread": 0}

    tools = {t.name: t for t in pt.build_peer_tools(Client())}
    tools["peer_inbox"].handler({}, ToolContext())
    assert calls == [("inbox", {"wait_s": 0, "mark_read": True, "limit": 20})]


# --------------------------------------------------------------------------- #
# PeerClient robustness against a misbehaving socket
# --------------------------------------------------------------------------- #
def _one_shot_server(path, reply: bytes, delay=0.0):
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.recv(65536)
        time.sleep(delay)
        try:
            conn.sendall(reply)
        except OSError:
            pass
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()


@pytest.mark.parametrize(
    "reply,want",
    [
        (b"", "bad response"),
        (b"garbage\n", "bad response"),
        (b"[1,2]\n", "bad response"),
        (b'{"ok": false}\n', "peer API call failed"),
        (b'{"ok": false, "error": "nope"}\n', "nope"),
        (b"\xff\xfe\n", "bad response"),
    ],
)
def test_client_bad_responses(short_home, reply, want):
    path = os.path.join(short_home, "s.sock")
    _one_shot_server(path, reply)
    c = pt.PeerClient({"MINDFLOCK_PEER_SOCKET": path, "MINDFLOCK_PEER_TOKEN": TOKEN})
    with pytest.raises(ToolError, match=want):
        c.call("whoami", {})


def test_client_response_too_large(short_home, monkeypatch):
    monkeypatch.setattr(pt, "MAX_RESPONSE", 1000)
    path = os.path.join(short_home, "s.sock")
    _one_shot_server(path, b"x" * 5000)
    c = pt.PeerClient({"MINDFLOCK_PEER_SOCKET": path, "MINDFLOCK_PEER_TOKEN": TOKEN})
    with pytest.raises(ToolError, match="too large"):
        c.call("whoami", {})


def test_client_timeout(short_home):
    path = os.path.join(short_home, "s.sock")
    _one_shot_server(path, b'{"ok":true,"result":{}}\n', delay=2)
    c = pt.PeerClient({"MINDFLOCK_PEER_SOCKET": path, "MINDFLOCK_PEER_TOKEN": TOKEN})
    with pytest.raises(ToolError, match="in time"):
        c.call("whoami", {}, timeout=0.3)


# --------------------------------------------------------------------------- #
# Entry points and the ordinary server
# --------------------------------------------------------------------------- #
def test_full_server_refuses_to_run_in_peer_mode(monkeypatch):
    """Fail closed: whichever entry point builds the ORDINARY toolset under
    MINDFLOCK_MCP_MODE=peer (e.g. ``mindflock mcp``), it refuses to serve."""
    import io

    from backend.mcp import build_server

    monkeypatch.setenv("MINDFLOCK_MCP_MODE", "peer")
    with pytest.raises(RuntimeError, match="non-peer"):
        build_server(stdin=io.BytesIO(), stdout=io.BytesIO())
    rogue = Tool("spawn_session", "x", "x", {"type": "object"}, lambda a, c: "x")
    with pytest.raises(RuntimeError):
        McpServer([rogue], io.BytesIO(), io.BytesIO())
    # the peer toolset itself is fine under the same env
    McpServer(pt.build_peer_tools(pt.PeerClient({})), io.BytesIO(), io.BytesIO())


@pytest.mark.parametrize("mode", ["", "normal", "PEERS", "peer-ish"])
def test_ordinary_server_unchanged_without_peer_mode(monkeypatch, mode):
    import io

    from backend.mcp import build_server

    if mode:
        monkeypatch.setenv("MINDFLOCK_MCP_MODE", mode)
    else:
        monkeypatch.delenv("MINDFLOCK_MCP_MODE", raising=False)
    srv = build_server(stdin=io.BytesIO(), stdout=io.BytesIO(), env={})
    assert "spawn_session" in srv.tools and "send_message" in srv.tools
    assert "peer_send" not in srv.tools


def test_peer_mode_detection():
    assert pt.is_peer_mode({"MINDFLOCK_MCP_MODE": "peer"})
    assert pt.is_peer_mode({"MINDFLOCK_MCP_MODE": " PEER "})
    assert not pt.is_peer_mode({})
    assert not pt.is_peer_mode({"MINDFLOCK_MCP_MODE": "peers"})


def _subprocess_env(extra):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("MINDFLOCK_", "CS_WEB"))
    }
    env.update(extra)
    return env


def test_module_entry_point_imports_nothing_networky():
    code = (
        "import sys\n"
        "import backend.mcp.__main__ as m\n"
        "from backend.mcp import peer_tools\n"
        "assert m.main is peer_tools.main\n"
        "peer_tools.build_peer_server(stdin=sys.stdin.buffer, "
        "stdout=sys.stdout.buffer, env={})\n"
        "print(' '.join(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-P", "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_subprocess_env({"MINDFLOCK_MCP_MODE": "peer"}),
        stdin=subprocess.DEVNULL,
    )
    assert out.returncode == 0, out.stderr
    mods = set(out.stdout.split())
    for bad in (
        "backend.mcp.tools",
        "backend.mcp.api",
        "backend.client",
        "backend.config",
        "backend.config.config",
        "backend.mcp.identity",
        "backend.mcp.policy",
        "backend.web",
        "http.client",
        "urllib.request",
        "ssl",
        "libtmux",
    ):
        assert bad not in mods, bad


def test_subprocess_end_to_end(env, live, share, svc):
    """``python -P -m backend.mcp`` with MINDFLOCK_MCP_MODE=peer serves the
    peer toolset over real stdio against the real agent API."""
    proc = subprocess.Popen(
        [sys.executable, "-P", "-m", "backend.mcp"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(env),
    )
    try:

        def rpc(i, method, params=None):
            msg = {"jsonrpc": "2.0", "id": i, "method": method}
            if params is not None:
                msg["params"] = params
            proc.stdin.write(json.dumps(msg).encode() + b"\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        init = rpc(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        )
        assert init["result"]["instructions"] == pt.PEER_INSTRUCTIONS
        names = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]
        assert tuple(names) == pt.PEER_TOOL_NAMES
        who = rpc(3, "tools/call", {"name": "whoami", "arguments": {}})
        assert (
            json.loads(who["result"]["content"][0]["text"])["share_id"]
            == share.share_id
        )
        bad = rpc(
            4, "tools/call", {"name": "spawn_session", "arguments": {"prompt": "x"}}
        )
        assert bad["error"]["code"] == -32602
    finally:
        proc.stdin.close()
        proc.wait(10)


def test_subprocess_without_env_fails_every_call():
    proc = subprocess.run(
        [sys.executable, "-P", "-m", "backend.mcp"],
        cwd=ROOT,
        input=(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "whoami", "arguments": {}},
                }
            )
            + "\n"
        ).encode(),
        capture_output=True,
        timeout=60,
        env=_subprocess_env({"MINDFLOCK_MCP_MODE": "peer"}),
    )
    lines = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
    call = next(x for x in lines if x.get("id") == 2)
    assert call["result"]["isError"] is True
    assert "not configured" in call["result"]["content"][0]["text"]
