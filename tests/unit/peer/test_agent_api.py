"""backend.peer.agent_api — the unix-socket API of the sandboxed agent.

The client here plays the (possibly prompt-injected) sandboxed agent: it can
send any bytes it likes to the socket. The invariants: only the token holder
gets an answer other than an error; every request is one bounded JSON line
with a strict schema; the inbox is always THIS share's session's; errors are
short strings, never tracebacks; the server can't be wedged by slow,
oversized or numerous clients.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import tempfile

import pytest

from backend.peer import agent_api as aa
from backend.peer import share as sh
from backend.web.core import mailbox
from tests.unit.peer.test_share import LINK, repo  # noqa: F401 — fixture

TOKEN = "t0k3n-" + "x" * 40
TITLE = "peer-share-session"
VICTIM = "orchestrator"


@pytest.fixture
def short_home(monkeypatch):
    """AF_UNIX paths max out at 108 bytes; pytest's tmp_path is too deep."""
    d = tempfile.mkdtemp(prefix="mfp", dir="/tmp")
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", d)
    yield d
    sh._rmtree(d)


@pytest.fixture
def share(short_home, repo):  # noqa: F811
    return sh.create_share(LINK, str(repo))


class FakeService:
    def __init__(self):
        self.calls = []
        self.connected = True
        self.name = "Bob"
        self.responses = {
            "msg": {"accepted": True},
            "diff": {"stat": [], "diff": "D", "truncated": False},
            "read_file": {
                "path": "x",
                "size": 1,
                "encoding": "utf-8",
                "content": "c",
                "truncated": False,
            },
            "list_files": {"files": ["a"], "truncated": False},
        }
        self.exc = None

    async def request(self, link_id, op, p, timeout=60):
        self.calls.append((link_id, op, p, timeout))
        if self.exc is not None:
            raise self.exc
        return self.responses[op]

    def is_connected(self, link_id):
        return self.connected

    def peer_name(self, link_id):
        return self.name


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
async def api(share, svc):
    a = aa.AgentApi(share, LINK, TOKEN, svc, TITLE)
    await a.start()
    yield a
    await a.stop()


async def raw_call(path, data: bytes, timeout=5.0, eof=False):
    r, w = await asyncio.open_unix_connection(path, limit=16 * 1024 * 1024)
    try:
        try:
            w.write(data)
            await w.drain()
            if eof:
                w.write_eof()
        except (BrokenPipeError, ConnectionResetError):
            pass
        line = await asyncio.wait_for(r.readline(), timeout)
    finally:
        w.close()
    return json.loads(line) if line else None


async def call(api_or_path, op, args=None, token=TOKEN, timeout=5.0, **extra):
    path = api_or_path if isinstance(api_or_path, str) else api_or_path.path
    req = {"token": token, "op": op}
    if args is not None:
        req["args"] = args
    req.update(extra)
    return await raw_call(path, (json.dumps(req) + "\n").encode(), timeout)


def is_error(resp, text=None):
    assert set(resp) == {"ok", "error"}, resp
    assert resp["ok"] is False and isinstance(resp["error"], str)
    assert len(resp["error"]) <= 300
    assert "Traceback" not in resp["error"] and 'File "' not in resp["error"]
    if text is not None:
        assert text in resp["error"], resp
    return True


# --------------------------------------------------------------------------- #
# Socket
# --------------------------------------------------------------------------- #
async def test_socket_is_0600_in_run(api, share):
    st = os.lstat(api.path)
    assert stat.S_ISSOCK(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert api.path == os.path.join(share.run, "agent.sock")
    assert stat.S_IMODE(os.stat(share.run).st_mode) == 0o700
    # no temp sockets left behind
    assert [n for n in os.listdir(share.run) if n.endswith(".sock")] == ["agent.sock"]


async def test_stop_removes_socket(share, svc):
    a = aa.AgentApi(share, LINK, TOKEN, svc, TITLE)
    await a.start()
    await a.stop()
    assert not os.path.exists(a.path)


async def test_stale_socket_replaced(share, svc):
    path = os.path.join(share.run, "agent.sock")
    s = socket.socket(socket.AF_UNIX)
    s.bind(path)
    s.close()
    a = aa.AgentApi(share, LINK, TOKEN, svc, TITLE)
    await a.start()
    try:
        assert (await call(a, "whoami", {}))["ok"] is True
    finally:
        await a.stop()


@pytest.mark.parametrize("kind", ["file", "symlink", "dir"])
async def test_refuses_to_clobber_non_socket(share, svc, tmp_path, kind):
    path = os.path.join(share.run, "agent.sock")
    target = tmp_path / "precious"
    target.write_text("keep")
    if kind == "file":
        open(path, "w").write("keep")
    elif kind == "symlink":
        os.symlink(target, path)
    else:
        os.mkdir(path)
    a = aa.AgentApi(share, LINK, TOKEN, svc, TITLE)
    with pytest.raises(RuntimeError):
        await a.start()
    assert target.read_text() == "keep"
    assert os.path.lexists(path)


@pytest.mark.parametrize("token", ["", "short", None, 123, "é" * 20])
def test_constructor_rejects_weak_token(share, svc, token):
    with pytest.raises(ValueError):
        aa.AgentApi(share, LINK, token, svc, TITLE)


def test_constructor_requires_title(share, svc):
    with pytest.raises(ValueError):
        aa.AgentApi(share, LINK, TOKEN, svc, "")


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "token",
    [
        "",
        "x",
        TOKEN[:-1],
        TOKEN + "x",
        TOKEN.upper(),
        " " + TOKEN,
        TOKEN + "\n",
        "é" * len(TOKEN),
        TOKEN.replace("x", "y"),
    ],
)
async def test_bad_token(api, svc, token):
    resp = await call(api, "send", {"text": "hi"}, token=token)
    assert is_error(resp, "unauthorized")
    assert svc.calls == []
    assert api.stats["bad_token"] == 1


@pytest.mark.parametrize("token", [None, 123, ["x"], {"t": TOKEN}, True])
async def test_non_string_token(api, svc, token):
    resp = await call(api, "whoami", {}, token=token)
    assert is_error(resp, "malformed")
    assert svc.calls == []


async def test_missing_token(api):
    resp = await raw_call(api.path, b'{"op":"whoami","args":{}}\n')
    assert is_error(resp, "malformed")


async def test_bad_token_checked_before_args(api):
    """An unauthenticated caller learns nothing about the op table."""
    resp = await call(api, "no-such-op", {"x": 1}, token="wrong")
    assert is_error(resp, "unauthorized")


async def test_wrong_peer_uid_refused(api, monkeypatch):
    monkeypatch.setattr(aa.os, "getuid", lambda: 4242)
    resp = await call(api, "whoami", {})
    assert is_error(resp, "unauthorized")


# --------------------------------------------------------------------------- #
# Framing / parsing
# --------------------------------------------------------------------------- #
async def test_oversize_line(api):
    big = (
        json.dumps(
            {"token": TOKEN, "op": "send", "args": {"text": "x" * (aa.MAX_LINE + 10)}}
        ).encode()
        + b"\n"
    )
    resp = await raw_call(api.path, big)
    assert resp is None or is_error(resp, "too large")
    # still serving
    assert (await call(api, "whoami", {}))["ok"] is True


async def test_oversize_without_newline(api):
    resp = await raw_call(api.path, b"{" + b" " * (aa.MAX_LINE + 100))
    assert resp is None or is_error(resp, "too large")
    assert (await call(api, "whoami", {}))["ok"] is True


async def test_exactly_at_limit_is_accepted(api, svc):
    base = {"token": TOKEN, "op": "send", "args": {"text": ""}}
    pad = aa.MAX_LINE - len(json.dumps(base))
    text = "x" * min(pad, 20000)
    resp = await call(api, "send", {"text": text})
    assert resp["ok"] is True


@pytest.mark.parametrize(
    "raw",
    [
        b"not json\n",
        b"\n",
        b"[]\n",
        b'"str"\n',
        b"null\n",
        b"42\n",
        b"{\n",
        b'{"token": "'
        + TOKEN.encode()
        + b'", "op": "whoami", "args": {}}',  # no \n, EOF
        b"\xff\xfe\n",
        b'{"token":"%s","op":"whoami","op":"send","args":{}}\n' % TOKEN.encode(),
        b'{"token":"%s","op":"whoami","args":{},"extra":1}\n' % TOKEN.encode(),
        b"[" * 100000 + b"\n",
        b'{"token":"%s","op":"whoami","args":%s}\n'
        % (TOKEN.encode(), b"[" * 5000 + b"]" * 5000),
    ],
)
async def test_malformed(api, svc, raw):
    resp = await raw_call(api.path, raw, eof=True)
    assert resp is None or is_error(resp)
    assert svc.calls == []
    assert (await call(api, "whoami", {}))["ok"] is True


async def test_one_request_per_connection(api, svc):
    two = (
        json.dumps({"token": TOKEN, "op": "send", "args": {"text": "a"}})
        + "\n"
        + json.dumps({"token": TOKEN, "op": "send", "args": {"text": "b"}})
        + "\n"
    )
    r, w = await asyncio.open_unix_connection(api.path)
    w.write(two.encode())
    await w.drain()
    first = await asyncio.wait_for(r.readline(), 5)
    rest = await asyncio.wait_for(r.read(), 5)
    w.close()
    assert json.loads(first)["ok"] is True
    assert rest == b""
    assert [c[2]["text"] for c in svc.calls] == ["a"]


# --------------------------------------------------------------------------- #
# Op table and argument validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "op",
    [
        "",
        "WHOAMI",
        "spawn_session",
        "send_message",
        "__init__",
        "_op_send",
        "stop",
        "_peer",
        "list_sessions",
        "peer_send",
        "read_file",
        "diff",
        7,
    ],
)
async def test_unknown_op(api, svc, op):
    resp = await call(api, op, {})
    assert is_error(resp)
    assert svc.calls == []


BAD_ARGS = [
    ("whoami", {"x": 1}),
    ("whoami", []),
    ("whoami", "x"),
    ("send", {}),
    ("send", {"text": ""}),
    ("send", {"text": None}),
    ("send", {"text": 5}),
    ("send", {"text": ["hi"]}),
    ("send", {"text": "x" * 20001}),
    ("send", {"text": "a\x00b"}),
    ("send", {"text": "hi", "reply_to": "a b"}),
    ("send", {"text": "hi", "reply_to": "x" * 65}),
    ("send", {"text": "hi", "reply_to": 5}),
    ("send", {"text": "hi", "reply_to": "../../x"}),
    ("send", {"text": "hi", "to": VICTIM}),
    ("send", {"text": "hi", "link_id": "cd" * 16}),
    ("inbox", {"wait_s": -1}),
    ("inbox", {"wait_s": 1501}),
    ("inbox", {"wait_s": "5"}),
    ("inbox", {"wait_s": True}),
    ("inbox", {"wait_s": float("nan")}),
    ("inbox", {"limit": 0}),
    ("inbox", {"limit": 51}),
    ("inbox", {"limit": 1.5}),
    ("inbox", {"limit": True}),
    ("inbox", {"mark_read": "yes"}),
    ("inbox", {"mark_read": 1}),
    ("inbox", {"title": VICTIM}),
    ("inbox", {"session": VICTIM}),
    ("inbox", {"to": VICTIM}),
    ("peer_diff", {"max_chars": 999}),
    ("peer_diff", {"max_chars": 200001}),
    ("peer_diff", {"max_chars": "5000"}),
    ("peer_read_file", {}),
    ("peer_read_file", {"path": ""}),
    ("peer_read_file", {"path": "x" * 1025}),
    ("peer_read_file", {"path": ["a"]}),
    ("peer_read_file", {"path": "a\x00"}),
    ("peer_list_files", {"path": "/"}),
    ("checkpoint", {"message": 5}),
    ("checkpoint", {"message": "x" * 2001}),
    ("checkpoint", {"share_id": "cd" * 16}),
]


@pytest.mark.parametrize("op,args", BAD_ARGS)
async def test_bad_args(api, svc, op, args):
    resp = await raw_call(
        api.path,
        (
            json.dumps({"token": TOKEN, "op": op, "args": args}, allow_nan=True) + "\n"
        ).encode(),
    )
    assert is_error(resp)
    assert svc.calls == []


def test_validate_args_defaults():
    assert aa.validate_args("inbox", {}) == {
        "wait_s": 0.0,
        "mark_read": True,
        "limit": 20,
    }
    assert aa.validate_args("peer_diff", {}) == {"max_chars": 50000}
    assert aa.validate_args("send", {"text": "a"}) == {"text": "a", "reply_to": None}
    assert set(aa.OPS) == {
        "whoami",
        "send",
        "inbox",
        "peer_diff",
        "peer_read_file",
        "peer_list_files",
        "checkpoint",
    }


async def test_args_optional_for_no_arg_ops(api):
    resp = await call(api, "whoami")  # no "args" key at all
    assert resp["ok"] is True


# --------------------------------------------------------------------------- #
# Ops
# --------------------------------------------------------------------------- #
async def test_whoami(api, share):
    resp = await call(api, "whoami", {})
    assert resp == {
        "ok": True,
        "result": {
            "share_id": share.share_id,
            "folder": share.work,
            "peer_name": "Bob",
            "connected": True,
        },
    }
    assert TOKEN not in json.dumps(resp)


async def test_whoami_sanitizes_peer_name(api, svc):
    svc.name = "Bob\x1b[31m\nEvil" + "x" * 100
    got = (await call(api, "whoami", {}))["result"]["peer_name"]
    assert "\x1b" not in got and "\n" not in got and len(got) <= 64


async def test_send(api, svc):
    resp = await call(api, "send", {"text": "hello", "reply_to": "abc_1-2"})
    assert resp["ok"] is True
    res = resp["result"]
    assert res["delivered"] is True
    assert set(res) == {"msg_id", "delivered"}
    ((link, op, p, timeout),) = svc.calls
    assert link == LINK and op == "msg"
    assert p == {"msg_id": res["msg_id"], "text": "hello", "reply_to": "abc_1-2"}
    import re

    assert re.match(r"^[A-Za-z0-9_-]{1,64}$", res["msg_id"])


async def test_send_not_accepted(api, svc):
    svc.responses["msg"] = {"accepted": False}
    assert (await call(api, "send", {"text": "x"}))["result"]["delivered"] is False
    svc.responses["msg"] = {"accepted": "yes"}  # anything but True is False
    assert (await call(api, "send", {"text": "x"}))["result"]["delivered"] is False


async def test_send_when_disconnected(api, svc):
    svc.connected = False
    resp = await call(api, "send", {"text": "hi"})
    assert is_error(resp, "not connected")
    assert svc.calls == []


@pytest.mark.parametrize(
    "exc,want",
    [
        (RuntimeError("not permitted"), "not permitted"),
        (asyncio.TimeoutError(), "did not answer"),
        (ConnectionError("link down\n\x1b[31mred"), "link down"),
        (ValueError(), "ValueError"),
        (RuntimeError("x" * 5000), None),
    ],
)
async def test_service_errors_are_mapped(api, svc, exc, want):
    svc.exc = exc
    resp = await call(api, "peer_diff", {"max_chars": 5000})
    assert is_error(resp, want)
    assert "\n" not in resp["error"] and "\x1b" not in resp["error"]


async def test_peer_bad_response(api, svc):
    svc.responses["list_files"] = ["not", "a", "dict"]
    assert is_error(await call(api, "peer_list_files", {}), "bad response")


async def test_proxied_ops(api, svc):
    d = await call(api, "peer_diff", {"max_chars": 1234})
    assert d == {"ok": True, "result": svc.responses["diff"]}
    f = await call(api, "peer_read_file", {"path": "../../etc/passwd"})
    assert f["ok"] is True  # the PEER enforces its own path rules
    lf = await call(api, "peer_list_files", {})
    assert lf["result"] == {"files": ["a"], "truncated": False}
    assert [(c[1], c[2]) for c in svc.calls] == [
        ("diff", {"max_chars": 1234}),
        ("read_file", {"path": "../../etc/passwd"}),
        ("list_files", {}),
    ]
    assert all(c[0] == LINK for c in svc.calls)


async def test_checkpoint_on_our_share(api, share):
    open(os.path.join(share.work, "agent.txt"), "w").write("work\n")
    resp = await call(api, "checkpoint", {"message": "from agent"})
    assert resp["ok"] is True
    head = sh.git(share, "rev-parse", "HEAD").stdout.decode().strip()
    assert resp["result"] == {"sha": head}
    assert (
        sh.git(share, "log", "-1", "--format=%s").stdout.decode().strip()
        == "from agent"
    )


async def test_checkpoint_share_error_is_passed(share, svc):
    def boom(s, m):
        raise sh.ShareError("git commit failed")

    a = aa.AgentApi(share, LINK, TOKEN, svc, TITLE, checkpoint=boom)
    async with a:
        assert is_error(await call(a, "checkpoint", {}), "git commit failed")


async def test_unexpected_error_is_generic(share, svc):
    def boom(s, m):
        raise KeyError("/home/secret/path")

    a = aa.AgentApi(share, LINK, TOKEN, svc, TITLE, checkpoint=boom)
    async with a:
        resp = await call(a, "checkpoint", {})
    assert resp == {"ok": False, "error": "internal error"}


# --------------------------------------------------------------------------- #
# Inbox — always and only this session's
# --------------------------------------------------------------------------- #
async def test_inbox_returns_only_own_messages(api):
    mailbox.post(VICTIM, "victim secret", sender="someone")
    mailbox.post(
        TITLE,
        "for you",
        sender="peer:Bob",
        data={"peer_msg_id": "p123", "link_id": LINK},
    )
    resp = await call(api, "inbox", {})
    msgs = resp["result"]["messages"]
    assert [m["text"] for m in msgs] == ["for you"]
    assert msgs[0]["untrusted"] is True
    assert msgs[0]["peer_msg_id"] == "p123"
    assert msgs[0]["from"] == "peer:Bob"
    # marked read by default; the victim's box untouched
    assert mailbox.unread_count(TITLE) == 0
    assert mailbox.unread_count(VICTIM) == 1
    assert (await call(api, "inbox", {}))["result"]["messages"] == []


@pytest.mark.parametrize(
    "extra",
    [{"title": VICTIM}, {"session_title": VICTIM}, {"to": VICTIM}, {"box": VICTIM}],
)
async def test_inbox_cannot_name_another_session(api, extra):
    mailbox.post(VICTIM, "victim secret", sender="someone")
    resp = await call(api, "inbox", extra)
    assert is_error(resp)
    # nor via a top-level key
    resp = await call(api, "inbox", {}, title=VICTIM)
    assert is_error(resp, "malformed")
    assert mailbox.unread_count(VICTIM) == 1


async def test_inbox_mark_read_false(api):
    mailbox.post(TITLE, "keep unread", sender="peer:Bob")
    resp = await call(api, "inbox", {"mark_read": False})
    assert len(resp["result"]["messages"]) == 1
    assert mailbox.unread_count(TITLE) == 1


async def test_inbox_limit(api):
    for i in range(5):
        mailbox.post(TITLE, "m%d" % i, sender="peer:Bob", delivery="inbox")
    resp = await call(api, "inbox", {"limit": 2})
    assert [m["text"] for m in resp["result"]["messages"]] == ["m0", "m1"]


async def test_inbox_wait_wakes_on_new_message(api):
    async def later():
        await asyncio.sleep(0.4)
        await asyncio.to_thread(mailbox.post, TITLE, "late", sender="peer:Bob")

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    poster = asyncio.ensure_future(later())
    resp = await call(api, "inbox", {"wait_s": 10}, timeout=15)
    await poster
    assert [m["text"] for m in resp["result"]["messages"]] == ["late"]
    assert loop.time() - t0 < 5


async def test_inbox_wait_times_out_empty(api):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    resp = await call(api, "inbox", {"wait_s": 0.6})
    assert resp["result"]["messages"] == []
    assert 0.5 <= loop.time() - t0 < 3


async def test_inbox_wait_ignores_other_boxes(api):
    async def later():
        await asyncio.sleep(0.2)
        await asyncio.to_thread(mailbox.post, VICTIM, "not yours", sender="x")

    poster = asyncio.ensure_future(later())
    resp = await call(api, "inbox", {"wait_s": 1.0})
    await poster
    assert resp["result"]["messages"] == []


async def test_inbox_wait_registers_waiter_and_stops_on_hangup(api):
    r, w = await asyncio.open_unix_connection(api.path)
    w.write(
        (
            json.dumps({"token": TOKEN, "op": "inbox", "args": {"wait_s": 60}}) + "\n"
        ).encode()
    )
    await w.drain()
    await asyncio.sleep(0.3)
    assert mailbox.waiter_active(TITLE)
    assert api._active == 1
    w.close()
    for _ in range(40):
        await asyncio.sleep(0.1)
        if api._active == 0:
            break
    assert api._active == 0  # the abandoned 60 s wait ended


# --------------------------------------------------------------------------- #
# Resource limits
# --------------------------------------------------------------------------- #
async def test_concurrent_connection_cap(share, svc):
    a = aa.AgentApi(share, LINK, TOKEN, svc, TITLE, max_conns=4, read_deadline=10)
    async with a:
        held = []
        for _ in range(4):
            held.append(await asyncio.open_unix_connection(a.path))
        await asyncio.sleep(0.2)
        assert a._active == 4
        resp = await call(a, "whoami", {})
        assert is_error(resp, "busy")
        assert a.stats["refused_busy"] == 1
        for _, w in held:
            w.close()
        for _ in range(30):
            await asyncio.sleep(0.05)
            if a._active == 0:
                break
        assert (await call(a, "whoami", {}))["ok"] is True


async def test_default_cap_is_16(api):
    assert api.max_conns == aa.MAX_CONNS == 16
    held = [await asyncio.open_unix_connection(api.path) for _ in range(16)]
    await asyncio.sleep(0.2)
    try:
        assert is_error(await call(api, "whoami", {}), "busy")
    finally:
        for _, w in held:
            w.close()


async def test_slow_client_times_out(share, svc):
    a = aa.AgentApi(share, LINK, TOKEN, svc, TITLE, read_deadline=0.3)
    async with a:
        r, w = await asyncio.open_unix_connection(a.path)
        w.write(b'{"token": "')
        await w.drain()
        line = await asyncio.wait_for(r.readline(), 3)
        assert is_error(json.loads(line), "timeout")
        assert await asyncio.wait_for(r.read(), 3) == b""
        w.close()


async def test_slowloris_drip_times_out(share, svc):
    a = aa.AgentApi(share, LINK, TOKEN, svc, TITLE, read_deadline=0.5)
    async with a:
        r, w = await asyncio.open_unix_connection(a.path)

        async def drip():
            try:
                for _ in range(50):
                    w.write(b" ")
                    await w.drain()
                    await asyncio.sleep(0.05)
            except (ConnectionError, OSError):
                pass

        dripper = asyncio.ensure_future(drip())
        line = await asyncio.wait_for(r.readline(), 3)
        dripper.cancel()
        assert is_error(json.loads(line), "timeout")
        w.close()


async def test_stop_cancels_long_waits(share, svc):
    a = aa.AgentApi(share, LINK, TOKEN, svc, TITLE)
    await a.start()
    r, w = await asyncio.open_unix_connection(a.path)
    w.write(
        (
            json.dumps({"token": TOKEN, "op": "inbox", "args": {"wait_s": 600}}) + "\n"
        ).encode()
    )
    await w.drain()
    await asyncio.sleep(0.2)
    await asyncio.wait_for(a.stop(), 6)
    assert await asyncio.wait_for(r.read(), 3) == b""
    w.close()


def test_short_home_cleanup_helper():
    """The fixture's rmtree copes with what tests leave in a share."""
    d = tempfile.mkdtemp(prefix="mfp", dir="/tmp")
    os.makedirs(os.path.join(d, "a", "b"))
    os.chmod(os.path.join(d, "a"), 0o500)
    sh._rmtree(d)
    assert not os.path.exists(d)
    shutil.rmtree(d, ignore_errors=True)
