"""The shared-MCP bridge (:mod:`backend.mcp.bridge`), end to end over HTTP.

A real stdio MCP server (the tiny script below, run as a subprocess) behind a
real bridge on 127.0.0.1, driven by two clients speaking streamable HTTP the
way Claude Code does (initialize → notifications/initialized → GET stream →
requests). Pins the multiplexing contract: the upstream initializes ONCE and
every client's initialize is answered from the cache under its own id; ids and
progress tokens are remapped so colliding client ids route back correctly;
list_changed fans out to every client; an upstream that dies is restarted on
the next request and clients keep their sessions; a server→client request is
refused with -32601 and counted; auth, the pre-initialize probe, unknown
sessions and idle reaping.
"""

from __future__ import annotations

import http.client
import json
import os
import queue
import socket
import sys
import textwrap
import threading
import time

import pytest

from backend.mcp import bridge

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX bridge")

TOKEN = "test-token"

FAKE_SERVER = textwrap.dedent(r"""
    import json, os, sys, time
    log = os.environ["FAKE_LOG"]
    def note(s):
        with open(log, "a") as f:
            f.write(s + "\n")
    note("start %d" % os.getpid())
    out_lock = None
    def send(m):
        sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
    pending_sample = {}
    for line in sys.stdin:
        m = json.loads(line)
        mid, meth = m.get("id"), m.get("method")
        if meth is None:  # a response to OUR request (sampling)
            pending_sample["reply"] = m
            continue
        note("msg %s" % meth)
        if mid is None:
            continue
        p = m.get("params") or {}
        if meth == "initialize":
            note("init caps=%s" % json.dumps(p.get("capabilities")))
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": p.get("protocolVersion"),
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "fake", "version": "1"}}})
        elif meth == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}})
        elif meth == "tools/call":
            name = p.get("name"); args = p.get("arguments") or {}
            if name == "echo":
                time.sleep(float(args.get("sleep", 0)))
                tok = (p.get("_meta") or {}).get("progressToken")
                if tok is not None:
                    send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": tok, "progress": 1}})
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps({"echo": args.get("v"), "pid": os.getpid(), "token": tok})}]}})
            elif name == "die":
                os._exit(3)
            elif name == "notify":
                send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": []}})
            elif name == "sample":
                send({"jsonrpc": "2.0", "id": "s1", "method": "sampling/createMessage", "params": {}})
                while "reply" not in pending_sample:
                    line2 = sys.stdin.readline()
                    if not line2:
                        sys.exit(0)
                    r = json.loads(line2)
                    if r.get("method") is None:
                        pending_sample["reply"] = r
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(pending_sample.pop("reply"))}]}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nf"}})
    """)


@pytest.fixture
def env(tmp_path):
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER)
    log = tmp_path / "fake.log"
    defs = {
        "fake": {
            "command": sys.executable,
            "args": [str(script)],
            "env": {"FAKE_LOG": str(log)},
        }
    }
    hub = bridge.Hub(str(tmp_path / "state"), definitions=lambda: defs)
    srv = bridge.make_server(hub, TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ns = type("Env", (), {})()
    ns.hub, ns.port, ns.log, ns.defs = hub, srv.server_address[1], log, defs
    yield ns
    srv.shutdown()
    hub.shutdown()


def _log_lines(env, prefix):
    try:
        return [ln for ln in env.log.read_text().splitlines() if ln.startswith(prefix)]
    except OSError:
        return []


def _post(port, body, sid=None, token=TOKEN, path="/mcp/fake", sse=True):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream" if sse else "application/json",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if sid:
        headers["Mcp-Session-Id"] = sid
    conn.request("POST", path, json.dumps(body), headers)
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    msgs = []
    if resp.getheader("Content-Type", "").startswith("text/event-stream"):
        for line in raw.splitlines():
            if line.startswith("data: "):
                msgs.append(json.loads(line[6:]))
    elif raw:
        msgs.append(json.loads(raw))
    return resp.status, resp.getheader("Mcp-Session-Id"), msgs


def _init(port, mid=1):
    status, sid, msgs = _post(
        port,
        {
            "jsonrpc": "2.0",
            "id": mid,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "t", "version": "1"},
                "capabilities": {"roots": {}, "sampling": {}, "elicitation": {}},
            },
        },
    )
    assert status == 200 and sid
    _post(port, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return sid, msgs[0]


def _call(port, sid, mid, name, **args):
    params = {"name": name, "arguments": args}
    status, _, msgs = _post(
        port,
        {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": params},
        sid,
    )
    return status, msgs


def _text(msg):
    return json.loads(msg["result"]["content"][0]["text"])


class _Stream:
    """A client's GET notification stream, read on a thread."""

    def __init__(self, port, sid):
        self.q: "queue.Queue" = queue.Queue()
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.conn.request(
            "GET",
            "/mcp/fake",
            headers={
                "Authorization": "Bearer " + TOKEN,
                "Accept": "text/event-stream",
                "Mcp-Session-Id": sid,
            },
        )
        self.resp = self.conn.getresponse()
        assert self.resp.status == 200
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.resp.fp.readline()
                if not line:
                    return
                line = line.decode().strip()
                if line.startswith("data: "):
                    self.q.put(json.loads(line[6:]))
        except Exception:  # noqa: BLE001
            return

    def get(self, timeout=10):
        return self.q.get(timeout=timeout)

    def close(self):
        try:
            # shutdown, not close: the response's file object holds the fd
            self.conn.sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001
            pass


def test_initialize_once_answered_from_cache_with_each_clients_id(env):
    sid_a, init_a = _init(env.port, mid=7)
    sid_b, init_b = _init(env.port, mid="abc")
    assert sid_a != sid_b
    assert init_a["id"] == 7 and init_b["id"] == "abc"
    assert init_a["result"] == init_b["result"]
    assert init_a["result"]["serverInfo"]["name"] == "fake"
    assert len(_log_lines(env, "start")) == 1  # ONE upstream process
    inits = _log_lines(env, "init")
    assert len(inits) == 1
    # The clients' roots/sampling/elicitation never reach the shared server.
    assert inits[0] == "init caps={}"
    # notifications/initialized reached it once, from the bridge.
    assert len(_log_lines(env, "msg notifications/initialized")) == 1


def test_colliding_client_ids_route_back_to_their_own_client(env):
    sid_a, _ = _init(env.port)
    sid_b, _ = _init(env.port)
    results = {}

    def run(sid, v, sleep):
        results[sid] = _call(env.port, sid, 5, "echo", v=v, sleep=sleep)

    ta = threading.Thread(target=run, args=(sid_a, "A", 0.5))
    tb = threading.Thread(target=run, args=(sid_b, "B", 0.0))
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    for sid, v in ((sid_a, "A"), (sid_b, "B")):
        status, msgs = results[sid]
        assert status == 200
        assert msgs[-1]["id"] == 5
        assert _text(msgs[-1])["echo"] == v


def test_progress_tokens_are_remapped_and_restored(env):
    sid, _ = _init(env.port)
    params = {
        "name": "echo",
        "arguments": {"v": 1},
        "_meta": {"progressToken": "tok-1"},
    }
    _, _, msgs = _post(
        env.port,
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": params},
        sid,
    )
    progress = [m for m in msgs if m.get("method") == "notifications/progress"]
    assert progress and progress[0]["params"]["progressToken"] == "tok-1"
    # The upstream saw a bridge-unique token, never the client's.
    assert _text(msgs[-1])["token"] != "tok-1"


def test_list_changed_fans_out_to_every_client(env):
    sid_a, _ = _init(env.port)
    sid_b, _ = _init(env.port)
    sa, sb = _Stream(env.port, sid_a), _Stream(env.port, sid_b)
    try:
        time.sleep(0.2)
        _call(env.port, sid_a, 1, "notify")
        assert sa.get()["method"] == "notifications/tools/list_changed"
        assert sb.get()["method"] == "notifications/tools/list_changed"
    finally:
        sa.close()
        sb.close()


def test_dead_upstream_is_restarted_and_sessions_survive(env):
    sid_a, _ = _init(env.port)
    sid_b, _ = _init(env.port)
    pid1 = _text(_call(env.port, sid_a, 1, "echo", v=1)[1][-1])["pid"]
    status, msgs = _call(env.port, sid_a, 2, "die")
    assert (
        msgs[-1]["error"]["code"] == bridge.INTERNAL_ERROR
    )  # in flight: fails cleanly
    sb = _Stream(env.port, sid_b)
    try:
        # B's session still works; the bridge restarted + re-initialized it.
        out = _call(env.port, sid_b, 3, "echo", v="after")[1][-1]
        assert _text(out)["echo"] == "after"
        assert _text(out)["pid"] != pid1
        assert len(_log_lines(env, "init")) == 2
        # ... and told its clients to refetch the lists.
        assert sb.get()["method"] == "notifications/tools/list_changed"
    finally:
        sb.close()


def test_server_to_client_requests_are_refused_and_counted(env):
    sid, _ = _init(env.port)
    _, msgs = _call(env.port, sid, 1, "sample")
    reply = _text(msgs[-1])  # what the fake server received for its request
    assert reply["id"] == "s1"
    assert reply["error"]["code"] == bridge.METHOD_NOT_FOUND
    status = env.hub.status()["upstreams"][0]
    assert status["refused_requests"] == {"sampling/createMessage": 1}


def test_ping_and_set_level_answered_locally(env):
    sid, _ = _init(env.port)
    for mid, method in ((1, "ping"), (2, "logging/setLevel")):
        _, _, msgs = _post(
            env.port, {"jsonrpc": "2.0", "id": mid, "method": method, "params": {}}, sid
        )
        assert msgs[-1] == {"jsonrpc": "2.0", "id": mid, "result": {}}
    assert not _log_lines(env, "msg ping")
    assert not _log_lines(env, "msg logging/setLevel")


def test_plain_json_reply_without_sse(env):
    sid, _ = _init(env.port)
    status, _, msgs = _post(
        env.port, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, sid, sse=False
    )
    assert status == 200 and msgs[0]["id"] == 4
    assert msgs[0]["result"]["tools"][0]["name"] == "echo"


def test_auth_probe_unknown_session_and_unknown_server(env):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert _post(env.port, body, token=None)[0] == 401
    assert _post(env.port, body, token="wrong")[0] == 401
    # The pre-initialize ``server/discover`` probe gets an immediate -32601.
    status, _, msgs = _post(
        env.port, {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
    )
    assert status == 200 and msgs[0]["error"]["code"] == bridge.METHOD_NOT_FOUND
    # An unknown session is a 404: the client re-initializes.
    assert _post(env.port, body, sid="nope")[0] == 404
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert _post(env.port, init, path="/mcp/missing")[0] == 404
    assert not _log_lines(env, "start")  # nothing spawned for any of it


def test_delete_ends_the_session(env):
    sid, _ = _init(env.port)
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=10)
    conn.request(
        "DELETE",
        "/mcp/fake",
        headers={"Authorization": "Bearer " + TOKEN, "Mcp-Session-Id": sid},
    )
    assert conn.getresponse().status == 200
    assert (
        _post(env.port, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, sid)[0]
        == 404
    )


def test_idle_upstream_is_stopped_after_its_grace(env, monkeypatch):
    sid, _ = _init(env.port)
    up = env.hub.status()["upstreams"][0]
    assert up["running"]
    env.hub.close_session(sid)
    now = time.time()
    env.hub.reap(now)  # starts the grace clock
    assert env.hub.status()["upstreams"][0]["running"]
    env.hub.reap(now + bridge.UPSTREAM_IDLE_S + 1)
    assert env.hub.status()["upstreams"] == []
    with pytest.raises(OSError):
        os.kill(up["pid"], 0)
    assert env.hub.idle_for(now + 10) >= 0


def test_live_stream_keeps_a_session_past_its_idle_window(env):
    sid, _ = _init(env.port)
    s = _Stream(env.port, sid)
    try:
        time.sleep(0.2)
        env.hub.reap(time.time() + bridge.SESSION_IDLE_S + 10)
        assert env.hub.session(sid) is not None
    finally:
        s.close()
    # Without the stream the same reap ends it.
    deadline = time.time() + 5
    while env.hub.session(sid).streams and time.time() < deadline:
        time.sleep(0.05)
    env.hub.reap(time.time() + bridge.SESSION_IDLE_S + 10)
    assert env.hub.session(sid) is None


def test_edited_definition_starts_a_fresh_upstream(env):
    sid_a, _ = _init(env.port)
    pid_a = _text(_call(env.port, sid_a, 1, "echo")[1][-1])["pid"]
    env.defs["fake"] = dict(env.defs["fake"], env=dict(env.defs["fake"]["env"], X="2"))
    sid_b, _ = _init(env.port)
    pid_b = _text(_call(env.port, sid_b, 1, "echo")[1][-1])["pid"]
    assert pid_a != pid_b
    # The old session keeps its own upstream until it drains.
    assert _text(_call(env.port, sid_a, 2, "echo")[1][-1])["pid"] == pid_a


def test_definition_digest_is_stable_and_field_sensitive():
    d = {"command": "x", "args": ["a"], "env": {"K": "v"}}
    assert bridge.definition_digest(d) == bridge.definition_digest(
        dict(d, type="stdio")
    )
    assert bridge.definition_digest(d) != bridge.definition_digest(dict(d, args=["b"]))


def test_binding_never_does_a_reverse_dns_lookup(monkeypatch, tmp_path):
    """The stock HTTPServer.server_bind calls socket.getfqdn(host); on macOS
    that can stall past the launcher's start timeout (the bridge then reads as
    never started). Binding must not touch it."""
    from backend.mcp import bridge

    def _boom(*_a, **_k):
        raise AssertionError("make_server did a reverse-DNS lookup")

    monkeypatch.setattr(socket, "getfqdn", _boom)
    srv = bridge.make_server(bridge.Hub(str(tmp_path)), "tok")
    try:
        assert srv.server_name == "127.0.0.1"
        assert srv.server_port == srv.server_address[1] > 0
    finally:
        srv.server_close()
