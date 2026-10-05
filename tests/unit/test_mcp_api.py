"""The MCP server's retrying API wrapper (backend.mcp.api)."""

from __future__ import annotations

import pytest

from backend import client
from backend.mcp.api import Api, quote_title
from backend.mcp.protocol import ToolError


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.naps = []

    def mono(self):
        return self.now

    def sleep(self, s):
        self.naps.append(s)
        self.now += s


@pytest.fixture()
def clock():
    return _Clock()


@pytest.fixture()
def wire(monkeypatch):
    """Scripted client.discover/get/post/delete/get_text."""
    state = {"discover": 0, "script": [], "calls": []}

    def discover(host=None, port=None, env=None):
        state["discover"] += 1
        return "http://127.0.0.1:8765"

    def fake(kind):
        def call(base, path, *a, **kw):
            state["calls"].append((kind, path))
            step = state["script"].pop(0) if state["script"] else {"ok": True}
            if isinstance(step, Exception):
                raise step
            return step

        return call

    monkeypatch.setattr(client, "discover", discover)
    for kind in ("get", "post", "delete", "get_text"):
        monkeypatch.setattr(client, kind, fake(kind))
    return state


def _api(clock):
    return Api(env={}, sleep=clock.sleep, clock=clock.mono)


def test_discovery_is_lazy_and_cached(wire, clock):
    api = _api(clock)
    assert wire["discover"] == 0
    api.get("/a")
    api.get("/b")
    assert wire["discover"] == 1


def test_connection_errors_are_retried_and_rediscovered(wire, clock):
    wire["script"] = [client.ServerNotFound(), client.ServerNotFound(), {"x": 1}]
    assert _api(clock).get("/a") == {"x": 1}
    assert wire["discover"] == 3  # forgot the address after each failure
    assert clock.naps == [0.25, 0.5]


def test_budget_exhausted_names_the_address(wire, clock):
    wire["script"] = [client.ServerNotFound()] * 50
    with pytest.raises(ToolError, match="not reachable at http://127.0.0.1:8765"):
        _api(clock).get("/a", retry_s=3)
    assert clock.now <= 3


def test_wait_budget_rides_out_a_restart(wire, clock):
    wire["script"] = [client.ServerNotFound()] * 8 + [[1]]
    assert _api(clock).get("/api/instances", retry_s=120) == [1]


def test_post_read_timeout_is_not_retried(wire, clock):
    wire["script"] = [client.RequestTimeout("slow")]
    with pytest.raises(ToolError, match="may still have done it"):
        _api(clock).post("/api/instances/x/messages", {"text": "hi"})
    assert [c for c in wire["calls"] if c[0] == "post"] == [
        ("post", "/api/instances/x/messages")
    ]


def test_post_refused_connection_is_retried(wire, clock):
    wire["script"] = [client.ServerNotFound(), {"ok": 1}]
    assert _api(clock).post("/p", {}) == {"ok": 1}


def test_get_read_timeout_is_retried(wire, clock):
    wire["script"] = [client.RequestTimeout("slow"), {"ok": 1}]
    assert _api(clock).get("/a") == {"ok": 1}


def test_api_errors_propagate_untouched(wire, clock):
    wire["script"] = [client.ApiError(409, "nope")]
    with pytest.raises(client.ApiError) as exc:
        _api(clock).get("/a")
    assert exc.value.status == 409
    assert clock.naps == []


def test_caller_sleep_is_used_for_backoff(wire, clock):
    naps = []
    wire["script"] = [client.ServerNotFound(), {"ok": 1}]
    _api(clock).get("/a", sleep=lambda s: (naps.append(s), clock.sleep(s)))
    assert naps == [0.25]


def test_text_and_delete_routes(wire, clock):
    wire["script"] = ["plain", {"ok": True}]
    api = _api(clock)
    assert api.get_text("/h") == "plain"
    assert api.delete("/d") == {"ok": True}
    assert [c[0] for c in wire["calls"]] == ["get_text", "delete"]


def test_config_is_cached(wire, clock):
    wire["script"] = [{"default_program": "claude", "caps": {}}]
    api = _api(clock)
    assert api.config()["default_program"] == "claude"
    assert api.config()["default_program"] == "claude"
    assert len(wire["calls"]) == 1


def test_instances_filters_garbage(wire, clock):
    wire["script"] = [[{"title": "a"}, "junk", None]]
    assert _api(clock).instances() == [{"title": "a"}]


def test_describe_before_discovery_uses_env():
    api = Api(env={"MINDFLOCK_HOST": "h", "MINDFLOCK_PORT": "9"})
    assert api.describe() == "http://h:9"


def test_quote_title():
    assert quote_title("a b") == "a%20b"
    assert quote_title("dev::x") == "dev::x"
    assert quote_title("a/b") == "a%2Fb"


# --------------------------------------------------------------------------- #
# Review fixes: auth refusals and post-send connection drops, over a real
# socket (the wire fixture would bypass client._send / probe entirely)
# --------------------------------------------------------------------------- #
import socket  # noqa: E402
import threading  # noqa: E402


class _RawServer:
    """A one-port TCP server whose per-connection behaviour is scripted."""

    def __init__(self, handler):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.requests = []
        self.heads = []
        self.handler = handler
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                conn.settimeout(2)
                try:
                    while b"\r\n\r\n" not in data:
                        data += conn.recv(4096)
                    head, _, body = data.partition(b"\r\n\r\n")
                    length = 0
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            length = int(line.split(b":")[1])
                    while len(body) < length:
                        body += conn.recv(4096)
                except OSError:
                    continue
                self.requests.append(head.split(b"\r\n")[0].decode())
                self.heads.append(head.decode("latin-1").lower())
                self.handler(conn, len(self.requests))

    def close(self):
        self.sock.close()


def _reply(conn, status, body=b'{"error": "unauthorized"}'):
    conn.sendall(
        b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d"
        b"\r\nConnection: close\r\n\r\n%s" % (status, len(body), body)
    )


@pytest.fixture()
def no_token(monkeypatch, tmp_path):
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "none.json"))
    client.reset_auth_token()
    yield
    client.reset_auth_token()


def test_a_401_at_discovery_is_an_auth_error_not_unreachable(no_token, clock):
    srv = _RawServer(lambda conn, n: _reply(conn, 401))
    try:
        api = Api(port=srv.port, env={}, sleep=clock.sleep, clock=clock.mono)
        with pytest.raises(ToolError) as exc:
            api.get("/api/instances", retry_s=120.0)
        assert "rejected the auth token" in str(exc.value)
        assert "not reachable" not in str(exc.value)
        assert clock.naps == []  # never retried
    finally:
        srv.close()


def test_probe_raises_auth_rejected_and_discover_passes_it_on(no_token):
    srv = _RawServer(lambda conn, n: _reply(conn, 403))
    try:
        with pytest.raises(client.AuthRejected):
            client.discover(port=srv.port, env={})
    finally:
        srv.close()


def test_a_post_whose_connection_drops_after_sending_is_not_replayed(no_token, clock):
    def handler(conn, n):
        if n == 1:
            return  # read the whole POST, then hang up without answering
        _reply(conn, 200, b'{"ok": true}')

    srv = _RawServer(handler)
    try:
        api = Api(env={}, sleep=clock.sleep, clock=clock.mono)
        api._base = "http://127.0.0.1:%d" % srv.port  # skip discovery
        with pytest.raises(ToolError) as exc:
            api.post("/api/instances/w/messages", {"text": "deploy now"})
        assert "may still have done it" in str(exc.value)
        assert srv.requests == ["POST /api/instances/w/messages HTTP/1.1"]
    finally:
        srv.close()


def test_a_get_whose_connection_drops_is_retried(wire, clock):
    wire["script"] = [client.ConnectionDropped("reset"), {"ok": 1}]
    assert _api(clock).get("/api/instances/x") == {"ok": 1}


# --------------------------------------------------------------------------- #
# Review round 2: the token goes only where it belongs, and only MindFlock's
# own gate reads as "rejected the auth token"
# --------------------------------------------------------------------------- #
@pytest.fixture()
def settings_token(monkeypatch, tmp_path):
    """This machine's server token in its settings file — no env override."""
    import json

    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"general": {"auth_token": "SECRET123"}}))
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(f))
    client.reset_auth_token()
    yield "SECRET123"
    client.reset_auth_token()


def _plain_401(conn, n):
    body = b"Unauthorized"
    conn.sendall(
        b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm=x\r\n"
        b"Content-Type: text/plain\r\nContent-Length: %d\r\nConnection: close"
        b"\r\n\r\n%s" % (len(body), body)
    )


def test_a_squatter_never_receives_the_token_and_is_not_a_server(settings_token):
    """Something else on the port that answers 401: not "MindFlock rejected
    your token" (uninstall refused, ls told the user to fix the token), and it
    never sees the local server's credential."""
    from backend import uninstall

    srv = _RawServer(_plain_401)
    try:
        assert client.probe("http://127.0.0.1:%d" % srv.port) is None
        assert uninstall.server_is_running("127.0.0.1", srv.port) is False
        with pytest.raises(client.ServerNotFound):
            client.discover("127.0.0.1", srv.port, env={})
        assert srv.heads and all("authorization" not in h for h in srv.heads)
    finally:
        srv.close()


def test_a_200_squatter_never_receives_the_token(settings_token):
    srv = _RawServer(lambda conn, n: _reply(conn, 200, b'{"hello": "world"}'))
    try:
        assert client.probe("http://127.0.0.1:%d" % srv.port) is None
        assert "authorization" not in srv.heads[0]
    finally:
        srv.close()


def test_the_token_is_sent_only_after_mindflocks_gate_answers(settings_token):
    def handler(conn, n):
        if n == 1:
            _reply(conn, 401)  # the gate's own {"error": "unauthorized"}
        else:
            _reply(conn, 200, b'{"default_program": "claude", "caps": {}}')

    srv = _RawServer(handler)
    try:
        cfg = client.probe("http://127.0.0.1:%d" % srv.port)
        assert cfg and cfg["default_program"] == "claude"
        assert "authorization" not in srv.heads[0]
        assert "authorization: bearer secret123" in srv.heads[1]
    finally:
        srv.close()


def test_the_settings_token_never_goes_to_a_remote_host(settings_token, monkeypatch):
    """``mindflock ls --host teammate-box`` used to send THIS machine's server
    token, in plaintext, to the other host."""
    assert client._token_for("http://127.0.0.1:8765/x", settings_token) == "SECRET123"
    assert client._token_for("http://localhost:8765/x", settings_token) == "SECRET123"
    assert client._token_for("http://[::1]:8765/x", settings_token) == "SECRET123"
    assert client._token_for("http://teammate-box:8765/x", settings_token) == ""
    assert client._token_for("http://100.64.0.7:8765/x", settings_token) == ""
    sent = []

    def fake_send(url, data, timeout, method, token):
        sent.append(token)
        return b'{"ok": true}'

    monkeypatch.setattr(client, "_send", fake_send)
    client.get("http://teammate-box:8765", "/api/instances")
    assert sent == [""]
    # An explicit MINDFLOCK_AUTH_TOKEN is the user naming a credential for
    # wherever they pointed the client.
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "EXPLICIT")
    client.reset_auth_token()
    client.get("http://teammate-box:8765", "/api/instances")
    assert sent[-1] == "EXPLICIT"


def test_a_remote_peers_refusal_is_not_blamed_on_the_local_token(wire, clock):
    """``device::title`` routes are proxied with the PEER's status: remote
    control off there is not the local token being wrong."""
    wire["script"] = [client.ApiError(403, "remote control is disabled on this device")]
    with pytest.raises(ToolError) as exc:
        _api(clock).get("/api/instances/laptop::api/output?lines=5")
    msg = str(exc.value)
    assert "remote control is disabled on this device" in msg
    assert "MINDFLOCK_AUTH_TOKEN" not in msg
    # A local route keeps the token hint, with the server's own words kept.
    wire["script"] = [client.ApiError(401, "token expired")]
    with pytest.raises(ToolError) as exc:
        _api(clock).get("/api/instances/api/output")
    assert "MINDFLOCK_AUTH_TOKEN" in str(exc.value)
    assert "token expired" in str(exc.value)
