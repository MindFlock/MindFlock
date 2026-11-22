"""Reach my devices from anywhere — the fixes of the independent security
review (docs/devices-anywhere.md, "Threat model summary"):

* M1 a tunnel connection's loopback-port mark is counted, so the late
  release of a closed connection never unmarks a live one on the same port;
* L1 the switch can't be flipped through the generic settings save, nor by
  settings sync;
* L2 a cloudflared never outlives its server (die-with-parent on Linux, a
  pid registry + an exact-match reaper everywhere), and the device proxy
  refuses what a stale tunnel would bring it;
* L3 the GitHub store never follows a redirect, and only writes to a repo
  the signed-in login owns that is private right now;
* L4 the tunnel's origin is this server's own port, known for certain, or
  the tunnel doesn't start;
* L5 open streams end when the member leaves or the group's key changes.

Loopback only: no GitHub, no Cloudflare, no real devices or ~/.mindflock.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from backend.peer import fleet_record as fr
from backend.peer import fleet_tunnel as ft
from backend.peer import identity, tunnel, tunnel_peers
from backend.peer.relay import RelayIngress
from backend.peer.transport import server_ssl_context
from backend.web.core import devices_anywhere as da
from backend.web.core import github_records as gr

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)

PATH = "/" + "h" * 26
KEYS = fr.derive("k" * 43, "0123456789abcdef", 1)


@pytest.fixture(autouse=True)
def _finalize_earlier_loops():
    """Finalize what earlier tests' (closed) event loops left behind, then
    keep the cycle collector out of this test. Relay/tunnel coroutines left
    pending when a test's loop closed are otherwise finalized by the
    collector at a random allocation DURING a later test, running their
    cleanup inside that test's loop — which cut a live stream of the later
    test now and then (seen on the base commit too, depending on file
    order). Test-only: a server runs one loop for its whole life."""
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


# --------------------------------------------------------------------------- #
# A device being reached: stand-in web server + TunnelServer + ingress
# --------------------------------------------------------------------------- #
class Origin:
    def __init__(self):
        self.ports = []

    async def _on(self, reader, writer):
        self.ports.append(writer.get_extra_info("peername")[1])
        try:
            while line := await reader.readline():
                writer.write(b"pong:" + line)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._on, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


@pytest.fixture
async def rig(tmp_path):
    origin = Origin()
    oport = await origin.start()
    ident = identity.load_or_create(str(tmp_path / "laptop-id"))
    state = {"keys": KEYS, "members": {"laptop", "rig"}}
    server = ft.TunnelServer(
        server_ssl_context(ident),
        ident.pub,
        keys=lambda: state["keys"],
        self_key=lambda: "laptop",
        members=lambda: state["members"],
        origin_port=lambda: oport,
    )
    ing = RelayIngress(server, PATH)
    port = await ing.start()

    class R:
        pass

    r = R()
    r.origin, r.server, r.state = origin, server, state
    r.url = "wss://127.0.0.1:%d%s" % (port, PATH)
    r.fp = ident.fingerprint().hex()
    yield r
    await ing.stop()
    origin.server.close()


async def _roundtrip(reader, writer, msg=b"hello\n") -> bytes:
    writer.write(msg)
    await writer.drain()
    return await asyncio.wait_for(reader.readline(), 5)


async def _ended(reader) -> bool:
    """Whether the stream reaches its end (what was already in flight is
    read past)."""
    try:
        async with asyncio.timeout(5):
            while await reader.read(65536):
                pass
        return True
    except (ConnectionError, OSError):
        return True
    except TimeoutError:
        return False


# --------------------------------------------------------------------------- #
# M1 — counted port marks
# --------------------------------------------------------------------------- #
def test_a_port_stays_marked_until_every_holder_released():
    port = 41234
    who = ("127.0.0.1", port)
    try:
        tunnel_peers.add(port)  # connection A
        tunnel_peers.add(port)  # connection B, handed the same ephemeral port
        tunnel_peers.discard(port)  # A's delayed release fires
        assert tunnel_peers.is_tunneled(who)  # B is live: still marked
        tunnel_peers.discard(port)  # B's release
        assert not tunnel_peers.is_tunneled(who)
        tunnel_peers.discard(port)  # a stray extra release is harmless
        assert not tunnel_peers.is_tunneled(who)
    finally:
        tunnel_peers._PORTS.pop(port, None)


async def test_late_release_of_a_closed_stream_never_unmarks_a_live_one(
    rig, monkeypatch
):
    """The collision, deterministically: stream A closes; before its grace
    ends a new stream B is handed A's very loopback port (simulated by
    taking B's hold on it); A's release fires — B stays marked."""
    monkeypatch.setattr(ft, "RELEASE_GRACE", 0.05)
    reader, writer = await ft.dial(
        rig.url, rig.fp, KEYS, "rig", "laptop", edge_ssl=None
    )
    assert await _roundtrip(reader, writer) == b"pong:hello\n"
    port = rig.origin.ports[0]
    who = ("127.0.0.1", port)
    assert tunnel_peers.is_tunneled(who)
    try:
        tunnel_peers.add(port)  # B takes the same port while A is closing
        writer.close()
        await asyncio.sleep(0.4)  # A's stream ended and its release fired
        assert rig.server.stats["open"] == 0
        assert tunnel_peers.is_tunneled(who), "a live connection was unmarked"
    finally:
        tunnel_peers.discard(port)  # B closes
    assert not tunnel_peers.is_tunneled(who)


# --------------------------------------------------------------------------- #
# L5 — streams end with the member / the key
# --------------------------------------------------------------------------- #
async def test_open_stream_ends_when_the_key_changes(rig):
    reader, writer = await ft.dial(
        rig.url, rig.fp, KEYS, "rig", "laptop", edge_ssl=None
    )
    assert await _roundtrip(reader, writer) == b"pong:hello\n"
    assert rig.server.sweep() == 0  # nothing changed: it stays
    assert await _roundtrip(reader, writer, b"still\n") == b"pong:still\n"
    rig.state["keys"] = fr.derive("n" * 43, "0123456789abcdef", 2)  # a removal
    assert rig.server.sweep() == 1
    assert await _ended(reader)
    writer.close()


async def test_open_stream_ends_when_the_member_leaves(rig):
    reader, writer = await ft.dial(
        rig.url, rig.fp, KEYS, "rig", "laptop", edge_ssl=None
    )
    assert await _roundtrip(reader, writer) == b"pong:hello\n"
    rig.state["members"] = {"laptop"}
    assert rig.server.sweep() == 1
    assert await _ended(reader)
    writer.close()


async def test_key_change_drops_streams_through_our_proxies(monkeypatch):
    class Proxy:
        dropped = 0

        def drop_streams(self):
            Proxy.dropped += 1
            return 1

    class Server:
        swept = 0

        def sweep(self):
            Server.swept += 1
            return 0

    da._reset_for_tests()
    try:
        da._S.server = Server()
        da._S.proxies = {"rig": Proxy()}
        da._revoke_stale_streams(KEYS)
        assert (Server.swept, Proxy.dropped) == (1, 0)  # first sight: no change
        da._revoke_stale_streams(KEYS)
        assert Proxy.dropped == 0
        da._revoke_stale_streams(fr.derive("n" * 43, "0123456789abcdef", 2))
        assert Proxy.dropped == 1 and Server.swept == 3
        # fleet_changed() (called on every save of the group's state) does
        # the same on the server's loop, at once.
        monkeypatch.setattr(da, "_keys", lambda: None)
        monkeypatch.setattr(da, "_LOOP", asyncio.get_running_loop())
        da.fleet_changed()
        await asyncio.sleep(0.01)
        assert Proxy.dropped == 2 and Server.swept == 4
    finally:
        da._reset_for_tests()


async def test_drop_streams_ends_a_piped_connection(rig):
    proxy = ft.DeviceProxy(
        "laptop", lambda: (rig.url, rig.fp, KEYS, "rig", "laptop"), edge_ssl=None
    )
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        head = b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % proxy.port
        assert await _roundtrip(reader, writer, head) == b"pong:GET / HTTP/1.1\r\n"
        assert proxy.drop_streams() == 1
        assert await _ended(reader)
        writer.close()
    finally:
        await proxy.stop()


# --------------------------------------------------------------------------- #
# L2 — the device proxy refuses what a stale tunnel would bring it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "head,ok",
    [
        (b"GET / HTTP/1.1\r\nHost: 127.0.0.1:5000\r\n\r\n", True),
        (b"GET / HTTP/1.1\r\nhost: LOCALHOST:5000\r\nAccept: */*\r\n\r\n", True),
        (b"GET / HTTP/1.1\r\nHost: abc.trycloudflare.com\r\n\r\n", False),
        (b"GET / HTTP/1.1\r\nHost: 127.0.0.1:5001\r\n\r\n", False),
        (b"GET / HTTP/1.1\r\n\r\n", False),  # no Host
        (
            b"GET / HTTP/1.1\r\nHost: 127.0.0.1:5000\r\nHost: 127.0.0.1:5000\r\n\r\n",
            False,
        ),
        (b"GET / HTTP/1.1\r\nHost: 127.0.0.1:5000\r\nCf-Ray: 1-AMS\r\n\r\n", False),
        (
            b"GET / HTTP/1.1\r\nHost: 127.0.0.1:5000\r\nCf-Connecting-Ip: 1.2.3.4\r\n\r\n",
            False,
        ),
        (
            b"GET / HTTP/1.1\r\nHost: 127.0.0.1:5000\r\nCDN-Loop: cloudflare\r\n\r\n",
            False,
        ),
        (
            b"GET / HTTP/1.1\r\nHost: 127.0.0.1:5000\r\nX-Forwarded-For: 1.2.3.4\r\n\r\n",
            False,
        ),
        (b"garbage\r\n\r\n", False),
        (b"GET / HTTP/1.1\r\nnocolon\r\n\r\n", False),
    ],
)
def test_local_request(head, ok):
    assert ft.local_request(head, 5000) is ok


async def test_proxy_refuses_a_relayed_request_without_dialing():
    dials = []

    async def dialer(*a, **k):  # pragma: no cover - must not be reached
        dials.append(a)
        raise OSError("no")

    results = []
    proxy = ft.DeviceProxy(
        "laptop",
        lambda: ("wss://x", "0" * 32, KEYS, "rig", "laptop"),
        edge_ssl=None,
        on_result=lambda ok, why: results.append((ok, why)),
        dialer=dialer,
    )
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(
            b"GET /api/instances HTTP/1.1\r\nHost: stale.trycloudflare.com\r\n"
            b"Cf-Ray: 1\r\nCf-Connecting-Ip: 203.0.113.9\r\n\r\n"
        )
        await writer.drain()
        assert await _ended(reader)
        writer.close()
        assert dials == [] and results == [(False, "refused_local")]
    finally:
        await proxy.stop()


# --------------------------------------------------------------------------- #
# L2 — cloudflared never outlives its server
# --------------------------------------------------------------------------- #
SLEEPER = "import time\nwhile True: time.sleep(0.1)\n"


def _spawn_sleeper(tmp_path, tag="cf"):
    script = tmp_path / ("%s.py" % tag)
    script.write_text(SLEEPER)
    argv = [sys.executable, str(script), "tunnel", "--url", "http://127.0.0.1:1"]
    proc = subprocess.Popen(argv, start_new_session=True)
    for _ in range(50):  # wait until its cmdline is the exec'd one
        facts = tunnel._proc_facts(proc.pid)
        if facts and facts[1] == argv:
            break
        time.sleep(0.02)
    return proc, argv


def _dead_pid() -> tuple:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid, time.time() - 5.0


def _record(folder, pid, argv, owner, owner_started, started=None):
    os.makedirs(folder, exist_ok=True)
    facts = tunnel._proc_facts(pid)
    doc = {
        "pid": pid,
        "started": started if started is not None else facts[0],
        "argv": argv,
        "owner": owner,
        "owner_started": owner_started,
    }
    path = os.path.join(folder, "%d-%d.json" % (owner, pid))
    with open(path, "w") as f:
        json.dump(doc, f)
    return path


def _gone(proc, wait=5.0) -> bool:
    try:
        proc.wait(wait)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_reaper_ends_a_tunnel_whose_server_died(tmp_path):
    proc, argv = _spawn_sleeper(tmp_path)
    try:
        folder = str(tmp_path / "pids")
        owner, owner_started = _dead_pid()
        path = _record(folder, proc.pid, argv, owner, owner_started)
        assert tunnel.reap_stale(folder) == 1
        assert _gone(proc)
        assert not os.path.exists(path)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_reaper_never_kills_on_a_mismatch(tmp_path):
    """Pid reused by something else (other command line, other start time):
    the record is dropped, the process left alone."""
    proc, argv = _spawn_sleeper(tmp_path)
    try:
        folder = str(tmp_path / "pids")
        owner, owner_started = _dead_pid()
        a = _record(folder, proc.pid, argv + ["--extra"], owner, owner_started)
        assert tunnel.reap_stale(folder) == 0 and not os.path.exists(a)
        b = _record(folder, proc.pid, argv, owner, owner_started, started=12345.0)
        assert tunnel.reap_stale(folder) == 0 and not os.path.exists(b)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_reaper_leaves_a_live_servers_tunnel_alone(tmp_path):
    """Two servers may share one state folder: a tunnel whose server still
    runs (here: the test runner's parent, with its real start time) stays."""
    proc, argv = _spawn_sleeper(tmp_path)
    try:
        folder = str(tmp_path / "pids")
        owner = os.getppid()
        owner_started = tunnel._proc_facts(owner)[0]
        path = _record(folder, proc.pid, argv, owner, owner_started)
        assert tunnel.reap_stale(folder) == 0
        assert proc.poll() is None and os.path.exists(path)
    finally:
        proc.kill()
        proc.wait()


def test_reaper_ends_what_a_previous_image_of_this_server_left(tmp_path):
    """A server that re-exec'd itself keeps its pid and start time; a record
    it wrote before (not in this image's own set) is stale."""
    proc, argv = _spawn_sleeper(tmp_path)
    try:
        folder = str(tmp_path / "pids")
        me = tunnel._proc_facts(os.getpid())
        path = _record(folder, proc.pid, argv, os.getpid(), me[0])
        tunnel._MINE.add(path)
        try:
            assert tunnel.reap_stale(folder) == 0  # this image's own: kept
        finally:
            tunnel._MINE.discard(path)
        assert tunnel.reap_stale(folder) == 1
        assert _gone(proc)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_reaper_ignores_junk(tmp_path):
    folder = tmp_path / "pids"
    folder.mkdir()
    (folder / "x.json").write_text("{not json")
    (folder / "y.json").write_text(json.dumps({"pid": 1, "owner": 1, "argv": "sh"}))
    (folder / "notes.txt").write_text("hi")
    assert tunnel.reap_stale(str(folder)) == 0
    assert sorted(os.listdir(folder)) == ["notes.txt"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux")
def test_child_dies_with_its_server(tmp_path):
    """A server SIGKILLed with no chance to clean up: its cloudflared (spawned
    with the tunnel's die-with-parent kwargs) is ended by the kernel."""
    out = tmp_path / "child.pid"
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text(SLEEPER)
    code = (
        "import os, signal, subprocess, sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from backend.peer import tunnel\n"
        "p = subprocess.Popen([sys.executable, %r], start_new_session=True,"
        " **tunnel._die_with_parent_kwargs())\n"
        "open(%r, 'w').write(str(p.pid))\n"
        "time.sleep(0.3)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    ) % (os.getcwd(), str(sleeper), str(out))
    server = subprocess.Popen([sys.executable, "-c", code])
    server.wait(20)
    child = int(out.read_text())
    deadline = time.time() + 5
    while time.time() < deadline and tunnel._proc_facts(child) is not None:
        time.sleep(0.05)
    alive = tunnel._proc_facts(child) is not None
    if alive:  # never leave it behind — exactly this pid
        os.kill(child, signal.SIGKILL)
    assert not alive


# --------------------------------------------------------------------------- #
# L3 — the GitHub store
# --------------------------------------------------------------------------- #
class Hub:
    """A fake GitHub API (the store's one HTTP seam)."""

    def __init__(self):
        self.repo = {
            "private": True,
            "permissions": {"push": True},
            "full_name": "me/mindflock-devices",
            "owner": {"login": "me"},
        }
        self.repo_status = 200
        self.files = {}
        self.puts = 0

    async def __call__(self, method, path, token, body=None):
        h = {"X-OAuth-Scopes": "repo"}
        if path == "/user":
            return 200, h, {"login": "me"}
        if path == "/repos/me/mindflock-devices":
            if self.repo_status != 200:
                return self.repo_status, dict(h, Location="https://x"), {}
            return 200, h, dict(self.repo)
        if method == "PUT":
            self.puts += 1
            return 201, h, {"content": {"sha": "s%d" % self.puts}}
        return 404, h, {}


def _hub_store(hub):
    async def tok():
        return "tok"

    return gr.GitHubStore(tok, http=hub)


NAME = "0" * 32 + ".rec"


async def test_a_moved_repo_is_not_followed():
    hub = Hub()
    hub.repo_status = 301
    with pytest.raises(gr.StoreError) as e:
        await _hub_store(hub).write(NAME, "mfd1.x")
    assert e.value.kind == "moved" and hub.puts == 0


@pytest.mark.parametrize(
    "repo",
    [
        {"full_name": "other/mindflock-devices", "owner": {"login": "other"}},
        {"full_name": "me/mindflock-devices", "owner": {"login": "other"}},
        {"full_name": "me/something-else", "owner": {"login": "me"}},
        {"full_name": "", "owner": None},
    ],
)
async def test_a_repo_not_owned_by_the_login_is_refused(repo):
    hub = Hub()
    hub.repo.update(repo)
    with pytest.raises(gr.StoreError) as e:
        await _hub_store(hub).write(NAME, "mfd1.x")
    assert e.value.kind == "not_owned" and hub.puts == 0


async def test_privacy_is_checked_again_before_every_write():
    hub = Hub()
    store = _hub_store(hub)
    await store.write(NAME, "mfd1.x")
    assert hub.puts == 1
    hub.repo["private"] = False  # made public since
    with pytest.raises(gr.StoreError) as e:
        await store.write(NAME, "mfd1.y")
    assert e.value.kind == "public_repo" and hub.puts == 1
    assert da.status  # (status words for the new kinds below)


async def test_new_store_errors_ask_for_github_in_the_status(monkeypatch):
    da._reset_for_tests()
    try:
        monkeypatch.setattr(da, "enabled", lambda: True)
        monkeypatch.setattr(da, "_keys", lambda: KEYS)
        da._S.tunnel_state = "up"
        for kind in ("not_owned", "moved"):
            da._note_github(gr.StoreError(kind, "why %s" % kind))
            st = da.status()
            assert st["state"] == "needs_github" and st["text"] == "why %s" % kind
    finally:
        da._reset_for_tests()


async def test_http_never_follows_a_redirect(monkeypatch):
    """The real HTTP seam against a loopback stand-in: a 301 comes back as
    is; the place it points to is never asked (with our token)."""
    from aiohttp import web

    hits = []

    async def moved(request):
        hits.append(("moved", request.headers.get("Authorization")))
        raise web.HTTPMovedPermanently(location="/elsewhere")

    async def elsewhere(request):  # pragma: no cover - must not be reached
        hits.append(("elsewhere", request.headers.get("Authorization")))
        return web.json_response({"private": True})

    app = web.Application()
    app.router.add_get("/repos/me/mindflock-devices", moved)
    app.router.add_get("/elsewhere", elsewhere)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(gr, "API", "http://127.0.0.1:%d" % port)
    try:
        status, _h, _d = await gr._http("GET", "/repos/me/mindflock-devices", "tok")
        assert status == 301
        assert hits == [("moved", "Bearer tok")]
    finally:
        await runner.cleanup()


# --------------------------------------------------------------------------- #
# L4 — the origin port is known for certain, or no tunnel
# --------------------------------------------------------------------------- #
def test_server_port_known_never_guesses(monkeypatch):
    from backend.web.core import mobile_access

    monkeypatch.delenv("UVICORN_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(sys, "argv", ["server"])
    assert mobile_access.server_port_known() is None
    assert mobile_access._server_port() == 8765  # the banner's guess stays
    monkeypatch.setenv("UVICORN_PORT", "9123")
    assert mobile_access.server_port_known() == 9123


async def test_origin_port_must_be_one_this_process_listens_on(monkeypatch):
    srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        monkeypatch.setattr(da, "_PORT_OK", {})
        monkeypatch.setattr(da, "_SERVER_PORT", None)
        assert da._origin_port() == 0
        monkeypatch.setattr(da, "_SERVER_PORT", 1)  # not ours
        assert da._origin_port() == 0
        monkeypatch.setattr(da, "_SERVER_PORT", port)
        assert da._origin_port() == port
    finally:
        srv.close()


async def test_tunnel_refuses_to_start_without_a_known_port(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(da.LOOPBACK_ENV, "1")
    da._reset_for_tests()
    try:
        da._set_store_for_tests(gr.DirStore(str(tmp_path / "store")))
        monkeypatch.setattr(da, "enabled", lambda: True)
        monkeypatch.setattr(da, "_keys", lambda: KEYS)
        monkeypatch.setattr(da, "_self_key", lambda: "laptop")
        monkeypatch.setattr(da, "_members", lambda: {"laptop", "rig"})
        monkeypatch.setattr(da, "_origin_port", lambda: 0)
        await da.apply()
        if da._S.start_task is not None:
            await da._S.start_task
        st = da.status()
        assert st["state"] == "failed" and da.PORT_UNKNOWN_TEXT in st["text"]
        assert da._S.ingress is None and da._S.url == ""
    finally:
        await da._shutdown()
        da._reset_for_tests()


# --------------------------------------------------------------------------- #
# L1 — only the device itself turns it on
# --------------------------------------------------------------------------- #
def test_generic_settings_save_cannot_turn_it_on():
    from backend.config.settings import load_settings
    from backend.web.addons import settings as settings_addon

    wrote = settings_addon._apply_post(
        {"general": {"devices_anywhere": True, "last_repo_path": "/tmp/x"}}
    )
    assert "general.devices_anywhere" not in wrote
    assert load_settings().general.devices_anywhere is False


def test_settings_sync_never_carries_it():
    from backend.web.core import settings_sync

    assert "devices_anywhere" in settings_sync.LOCAL["general"]
    assert "general.devices_anywhere" not in settings_sync.paths()
    assert settings_sync._valid_unit("general.devices_anywhere") is False


def test_api_settings_post_leaves_it_off(monkeypatch):
    """The whole route: another fleet member (or anything may_configure
    lets in) posting the field to /api/settings changes nothing."""
    from fastapi.testclient import TestClient

    from backend.config.settings import load_settings
    from backend.web import server

    client = TestClient(server.app)
    r = client.post("/api/settings", json={"general": {"devices_anywhere": True}})
    assert r.status_code == 200, r.text
    assert load_settings().general.devices_anywhere is False


@pytest.mark.parametrize("how", ["relayed", "tunneled"])
def test_dedicated_route_refuses_other_devices(how, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.config.settings import load_settings
    from backend.web import server
    from backend.web.core import auth

    headers = {}
    if how == "relayed":
        headers["X-MindFlock-Remote"] = "rig"
    else:
        monkeypatch.setattr(auth, "tunneled", lambda scope: True)
    client = TestClient(server.app)
    r = client.post("/api/devices/anywhere", json={"on": True}, headers=headers)
    assert r.status_code in (401, 403), r.text
    assert load_settings().general.devices_anywhere is False
