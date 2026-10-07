"""Unix sockets under a deep $HOME: share run/ paths can exceed sun_path's
108 bytes. Every bind and connect (egress proxy, agent API, the in-sandbox
bridge, the peer-mode MCP client) must still work."""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from backend.mcp.peer_tools import _connect_unix
from backend.peer import bridge, paths
from backend.peer.egress import EgressProxy


@pytest.fixture
def deep_dir(tmp_path):
    d = tmp_path / ("x" * 60) / ("y" * 60)
    d.mkdir(parents=True)
    assert len(os.fsencode(str(d / "agent.sock"))) > 108
    return d


def test_unix_addr_short_path_unchanged(tmp_path):
    p = str(tmp_path / "s.sock")[:90] if len(str(tmp_path)) < 80 else "/tmp/s.sock"
    with paths.unix_addr(p) as addr:
        assert addr == p


def test_bind_and_connect_past_sun_path(deep_dir):
    path = str(deep_dir / "agent.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with paths.unix_addr(path) as addr:
        srv.bind(addr)
    srv.listen(1)
    assert os.path.exists(path)
    cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _connect_unix(cli, path)
    conn, _ = srv.accept()
    cli.sendall(b"hi")
    assert conn.recv(2) == b"hi"
    for s in (cli, conn, srv):
        s.close()
    # the bridge's helper reaches the same entry
    assert bridge._short(path).startswith("/proc/self/fd/")


def test_egress_proxy_starts_on_deep_path(deep_dir):
    path = str(deep_dir / "egress.sock")

    async def go():
        proxy = EgressProxy(path, ["api.anthropic.com"])
        await proxy.start()
        try:
            assert os.stat(path).st_mode & 0o777 == 0o600
            r, w = await asyncio.open_unix_connection(bridge._short(path))
            w.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            await w.drain()
            line = await asyncio.wait_for(r.readline(), 5)
            assert line.startswith(b"HTTP/1.1 4")  # refused, but reachable
            w.close()
        finally:
            await proxy.stop()
        assert not os.path.exists(path)

    asyncio.run(go())
