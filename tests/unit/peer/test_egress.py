"""EgressProxy: the sandbox's only way out. Adversarial unit tests."""

from __future__ import annotations

import asyncio
import os
import stat

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.peer import egress
from backend.peer.egress import (
    ConnectError,
    EgressProxy,
    host_allowed,
    is_public_ip,
    parse_connect,
)

ALLOW = ["api.anthropic.com", ".example.org"]
PUBLIC = "93.184.216.34"


# --------------------------------------------------------------------------
# parse_connect


@pytest.mark.parametrize(
    "head,expect",
    [
        (b"CONNECT api.anthropic.com:443 HTTP/1.1", ("api.anthropic.com", 443)),
        (
            b"CONNECT API.Anthropic.COM:443 HTTP/1.1\r\nHost: x\r\nUser-Agent: y",
            ("api.anthropic.com", 443),
        ),
        (b"CONNECT a.bc:80 HTTP/1.0", ("a.bc", 80)),
    ],
)
def test_parse_connect_ok(head, expect):
    assert parse_connect(head) == expect


@pytest.mark.parametrize(
    "head,status",
    [
        (b"GET http://api.anthropic.com/ HTTP/1.1", 405),
        (b"POST api.anthropic.com:443 HTTP/1.1", 405),
        (b"connect api.anthropic.com:443 HTTP/1.1", 405),
        (b"CONNECT  api.anthropic.com:443 HTTP/1.1", 400),
        (b"CONNECT api.anthropic.com:443", 400),
        (b"CONNECT api.anthropic.com:443 HTTP/2", 400),
        (b"CONNECT api.anthropic.com HTTP/1.1", 400),
        (b"CONNECT api.anthropic.com: HTTP/1.1", 400),
        (b"CONNECT api.anthropic.com:0443 HTTP/1.1", 400),
        (b"CONNECT api.anthropic.com:+443 HTTP/1.1", 400),
        (b"CONNECT api.anthropic.com:443443 HTTP/1.1", 400),
        (b"CONNECT api.anthropic.com:\xd9\xa4\xd9\xa4\xd9\xa3 HTTP/1.1", 400),
        (b"CONNECT 127.0.0.1:443 HTTP/1.1", 400),
        (b"CONNECT 10.0.0.1:443 HTTP/1.1", 400),
        (b"CONNECT [::1]:443 HTTP/1.1", 400),
        (b"CONNECT [::ffff:127.0.0.1]:443 HTTP/1.1", 400),
        (b"CONNECT 2130706433:443 HTTP/1.1", 400),
        (b"CONNECT 0x7f.1:443 HTTP/1.1", 400),
        (b"CONNECT localhost:443 HTTP/1.1", 400),
        (b"CONNECT api.anthropic.com.:443 HTTP/1.1", 400),
        (b"CONNECT user@api.anthropic.com:443 HTTP/1.1", 400),
        (b"CONNECT api_anthropic.com:443 HTTP/1.1", 400),
        (b"CONNECT -api.anthropic.com:443 HTTP/1.1", 400),
        (b"CONNECT api.anthropic.com\x00.evil.com:443 HTTP/1.1", 400),
        (b"CONNECT api.anthropic.com:443 HTTP/1.1\nHost: x", 400),
        (b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost x", 400),
        (b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n Folded: x", 400),
        (b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nX: \x01", 400),
        (b"CONNECT \xc3\xa4pi.anthropic.com:443 HTTP/1.1", 400),
        (b"CONNECT api.anthropic.com:443 HTTP/1.1" + b"\r\nX: y" * 100, 431),
        (b"", 400),
    ],
)
def test_parse_connect_rejects(head, status):
    with pytest.raises(ConnectError) as ei:
        parse_connect(head)
    assert ei.value.status == status


@settings(max_examples=2000, deadline=None)
@given(st.binary(max_size=600))
def test_parse_connect_fuzz_bytes(data):
    try:
        host, port = parse_connect(data)
    except ConnectError as exc:
        assert exc.status in (400, 405, 431)
        return
    assert egress._HOST_RE.match(host) and host == host.lower()
    assert 0 <= port <= 65535
    assert data.startswith(b"CONNECT ")


_label = st.from_regex(r"[A-Za-z0-9\-_.@\[\]:%\x00 ]{0,20}", fullmatch=True)


@settings(max_examples=2000, deadline=None)
@given(
    method=st.sampled_from(["CONNECT", "GET", "connect", "CONNECT ", ""]),
    host=_label,
    port=st.one_of(st.integers(-5, 70000).map(str), st.text(max_size=6)),
    version=st.sampled_from(["HTTP/1.1", "HTTP/1.0", "HTTP/1.2", "", "http/1.1"]),
    headers=st.lists(st.text(max_size=30), max_size=5),
)
def test_parse_connect_fuzz_structured(method, host, port, version, headers):
    raw = f"{method} {host}:{port} {version}" + "".join("\r\n" + h for h in headers)
    try:
        data = raw.encode("utf-8")
    except UnicodeEncodeError:
        return
    try:
        h, p = parse_connect(data)
    except ConnectError:
        return
    # Anything accepted is a plain DNS name and the exact port that was sent.
    assert method == "CONNECT" and version in ("HTTP/1.1", "HTTP/1.0")
    assert h == host.lower() and str(p) == port
    assert egress._HOST_RE.match(h)
    assert not any(c in h for c in "@[]:% \x00_")


# --------------------------------------------------------------------------
# allow-list and address checks


@pytest.mark.parametrize(
    "host,ok",
    [
        ("api.anthropic.com", True),
        ("x.api.anthropic.com", False),
        ("anthropic.com", False),
        ("api.anthropic.com.evil.com", False),
        ("evilapi.anthropic.com", False),
        ("a.example.org", True),
        ("a.b.example.org", True),
        ("example.org", False),
        ("badexample.org", False),
        ("example.org.evil.com", False),
    ],
)
def test_host_allowed(host, ok):
    assert host_allowed(host, ALLOW) is ok


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "127.1.2.3",
        "0.0.0.0",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "100.127.255.254",
        "224.0.0.1",
        "239.255.255.250",
        "240.0.0.1",
        "255.255.255.255",
        "192.0.2.1",
        "198.18.0.1",
        "::",
        "::1",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd12:3456::1",
        "ff02::1",
        "fec0::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::7f00:1",
        "64:ff9b::a00:1",
        "2002:7f00:1::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "2001:db8::1",
        "not-an-ip",
        "",
    ],
)
def test_non_public_ips_refused(ip):
    assert not is_public_ip(ip)


@pytest.mark.parametrize(
    "ip",
    [PUBLIC, "1.1.1.1", "2606:4700:4700::1111", "::ffff:1.1.1.1", "64:ff9b::101:101"],
)
def test_public_ips_allowed(ip):
    assert is_public_ip(ip)


# --------------------------------------------------------------------------
# the proxy, end to end over a unix socket


class Upstream:
    """A local TCP echo server standing in for the real API host."""

    def __init__(self, host="127.0.0.1"):
        self.host = host
        self.server = None
        self.port = None
        self.connections = 0

    async def __aenter__(self):
        async def echo(r, w):
            self.connections += 1
            try:
                while data := await r.read(65536):
                    w.write(data)
                    await w.drain()
            finally:
                w.close()

        self.server = await asyncio.start_server(echo, self.host, 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self.server.close()
        await self.server.wait_closed()


def _resolver(table):
    async def resolve(host):
        val = table.get(host, [])
        if isinstance(val, Exception):
            raise val
        return val

    return resolve


@pytest.fixture
def sock_path(tmp_path):
    d = tmp_path / "run"
    d.mkdir(mode=0o700)
    return str(d / "egress.sock")


async def _talk(path, payload: bytes, *, read_all=True, timeout=5.0):
    r, w = await asyncio.open_unix_connection(path)
    w.write(payload)
    await w.drain()
    data = await asyncio.wait_for(r.read(65536) if not read_all else r.read(), timeout)
    w.close()
    return data


async def test_allowed_tunnel_relays_bytes_and_leftover(sock_path):
    async with Upstream() as up:
        px = EgressProxy(
            sock_path,
            ALLOW,
            resolver=_resolver({"api.anthropic.com": ["127.0.0.1"]}),
            ip_ok=lambda ip: ip == "127.0.0.1",
            upstream_port=up.port,
        )
        await px.start()
        try:
            assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600
            r, w = await asyncio.open_unix_connection(sock_path)
            # Bytes pipelined right after the header must reach upstream.
            w.write(
                b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\nEARLY"
            )
            await w.drain()
            status = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
            assert status.startswith(b"HTTP/1.1 200")
            assert await asyncio.wait_for(r.readexactly(5), 5) == b"EARLY"
            w.write(b"ping")
            await w.drain()
            assert await asyncio.wait_for(r.readexactly(4), 5) == b"ping"
            w.close()
            assert px.stats["allowed"] == 1
            assert ("api.anthropic.com", "allow") in px.decisions
        finally:
            await px.stop()
        assert not os.path.exists(sock_path)


async def _expect_status(sock_path, payload, status, **kw):
    px = EgressProxy(sock_path, ALLOW, **kw)
    await px.start()
    try:
        data = await _talk(sock_path, payload)
        assert data.startswith(f"HTTP/1.1 {status} ".encode()), data
        return px
    finally:
        await px.stop()


async def test_denied_host(sock_path):
    called = []

    async def resolve(host):
        called.append(host)
        return [PUBLIC]

    px = await _expect_status(
        sock_path, b"CONNECT evil.com:443 HTTP/1.1\r\n\r\n", 403, resolver=resolve
    )
    assert called == []  # never even resolved
    assert px.stats["denied"] == 1


async def test_bare_suffix_and_lookalikes_denied(sock_path):
    for host in (b"example.org", b"example.org.evil.com", b"xapi.anthropic.com"):
        await _expect_status(
            sock_path,
            b"CONNECT " + host + b":443 HTTP/1.1\r\n\r\n",
            403,
            resolver=_resolver({}),
        )


@pytest.mark.parametrize("port", [80, 22, 8765, 444, 1])
async def test_non_443_denied(sock_path, port):
    await _expect_status(
        sock_path,
        f"CONNECT api.anthropic.com:{port} HTTP/1.1\r\n\r\n".encode(),
        403,
        resolver=_resolver({"api.anthropic.com": [PUBLIC]}),
    )


@pytest.mark.parametrize(
    "target", [b"127.0.0.1:443", b"10.0.0.1:443", b"[::1]:443", b"169.254.169.254:443"]
)
async def test_ip_literals_refused(sock_path, target):
    await _expect_status(sock_path, b"CONNECT " + target + b" HTTP/1.1\r\n\r\n", 400)


@pytest.mark.parametrize(
    "ips",
    [
        ["127.0.0.1"],
        ["10.0.0.5"],
        ["169.254.169.254"],
        ["100.64.1.1"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["fd00::1"],
        [PUBLIC, "127.0.0.1"],  # mixed: ANY non-global address refuses
        ["127.0.0.1", PUBLIC],
        [PUBLIC, "fe80::1"],
    ],
)
async def test_resolution_to_non_global_refused(sock_path, ips):
    async with Upstream() as up:
        px = await _expect_status(
            sock_path,
            b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n",
            403,
            resolver=_resolver({"api.anthropic.com": ips}),
            upstream_port=up.port,
        )
        assert up.connections == 0
        assert px.stats["denied"] == 1


async def test_resolution_failure_and_empty(sock_path):
    await _expect_status(
        sock_path,
        b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n",
        502,
        resolver=_resolver({"api.anthropic.com": OSError("nxdomain")}),
    )
    await _expect_status(
        sock_path,
        b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n",
        502,
        resolver=_resolver({}),
    )


async def test_connects_to_checked_ip_never_reresolves(sock_path):
    calls = []

    async def resolve(host):
        calls.append(host)
        # A rebinding resolver: public first, loopback after.
        return ["127.0.0.1"] if len(calls) > 1 else ["127.0.0.2"]

    async with Upstream("127.0.0.2") as up:
        # 127.0.0.2 is exempted only through the test hook; 127.0.0.1 never.
        px = EgressProxy(
            sock_path,
            ALLOW,
            resolver=resolve,
            ip_ok=lambda ip: ip == "127.0.0.2",
            upstream_port=up.port,
        )
        await px.start()
        try:
            r, w = await asyncio.open_unix_connection(sock_path)
            w.write(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
            await w.drain()
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 200")
            w.close()
        finally:
            await px.stop()
    assert calls == ["api.anthropic.com"]


async def test_upstream_unreachable(sock_path):
    await _expect_status(
        sock_path,
        b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n",
        502,
        resolver=_resolver({"api.anthropic.com": ["127.0.0.1"]}),
        ip_ok=lambda ip: True,
        upstream_port=1,
    )


@pytest.mark.parametrize(
    "payload,status",
    [
        (
            b"GET http://api.anthropic.com/ HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
            405,
        ),
        (b"GET / HTTP/1.1\r\n\r\n", 405),
        (b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03\r\n\r\n", 400),
        (b"CONNECT api.anthropic.com:443\r\n\r\n", 400),
        (b"\r\n\r\n", 400),
        (b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n", 400),  # EOF before blank line
    ],
)
async def test_malformed_requests(sock_path, payload, status):
    r = _resolver({"api.anthropic.com": [PUBLIC]})
    px = EgressProxy(sock_path, ALLOW, resolver=r)
    await px.start()
    try:
        rr, w = await asyncio.open_unix_connection(sock_path)
        w.write(payload)
        await w.drain()
        if w.can_write_eof():
            w.write_eof()
        data = await asyncio.wait_for(rr.read(), 5)
        assert data.startswith(f"HTTP/1.1 {status} ".encode()), data
        w.close()
    finally:
        await px.stop()


async def test_header_flood_refused(sock_path):
    px = EgressProxy(
        sock_path, ALLOW, resolver=_resolver({"api.anthropic.com": [PUBLIC]})
    )
    await px.start()
    try:
        r, w = await asyncio.open_unix_connection(sock_path)
        w.write(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n")
        try:
            for _ in range(200):
                w.write(b"X-Flood: " + b"a" * 100 + b"\r\n")
                await w.drain()
        except (ConnectionError, OSError):
            pass
        data = await asyncio.wait_for(r.read(), 5)
        assert data.startswith(b"HTTP/1.1 431 ")
        w.close()
    finally:
        await px.stop()
    # A single huge line with no CRLF at all.
    px = EgressProxy(sock_path, ALLOW)
    await px.start()
    try:
        r, w = await asyncio.open_unix_connection(sock_path)
        try:
            w.write(b"C" * 100_000)
            await w.drain()
        except (ConnectionError, OSError):
            pass
        data = await asyncio.wait_for(r.read(), 5)
        assert data.startswith(b"HTTP/1.1 431 ")
    finally:
        await px.stop()


async def test_slowloris_times_out(sock_path):
    px = EgressProxy(sock_path, ALLOW, header_timeout=0.5)
    await px.start()
    try:
        r, w = await asyncio.open_unix_connection(sock_path)
        t0 = asyncio.get_running_loop().time()
        # Trickle a few bytes, never finishing the header.
        for ch in b"CONNE":
            w.write(bytes([ch]))
            await w.drain()
            await asyncio.sleep(0.05)
        data = await asyncio.wait_for(r.read(), 5)
        assert data.startswith(b"HTTP/1.1 408 ")
        assert asyncio.get_running_loop().time() - t0 < 4
        assert px.stats["active"] == 0
    finally:
        await px.stop()


async def test_concurrency_cap(sock_path):
    px = EgressProxy(sock_path, ALLOW, max_tunnels=3, header_timeout=5)
    await px.start()
    held = []
    try:
        for _ in range(3):
            held.append(await asyncio.open_unix_connection(sock_path))
        await asyncio.sleep(0.1)
        assert px.stats["active"] == 3
        data = await _talk(sock_path, b"")
        assert data.startswith(b"HTTP/1.1 503 ")
        assert px.stats["busy"] == 1
    finally:
        for _r, w in held:
            w.close()
        await px.stop()


async def test_tunnel_time_cap(sock_path):
    async with Upstream() as up:
        px = EgressProxy(
            sock_path,
            ALLOW,
            resolver=_resolver({"api.anthropic.com": ["127.0.0.1"]}),
            ip_ok=lambda ip: ip == "127.0.0.1",
            upstream_port=up.port,
            tunnel_max_s=0.5,
        )
        await px.start()
        try:
            r, w = await asyncio.open_unix_connection(sock_path)
            w.write(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
            await w.drain()
            await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
            assert await asyncio.wait_for(r.read(), 5) == b""  # closed by the cap
        finally:
            await px.stop()


async def test_refuses_to_replace_non_socket(tmp_path):
    p = tmp_path / "egress.sock"
    p.write_text("not a socket")
    with pytest.raises(RuntimeError):
        await EgressProxy(str(p), ALLOW).start()
    assert p.read_text() == "not a socket"


async def test_stale_socket_replaced(sock_path):
    px = EgressProxy(sock_path, ALLOW)
    await px.start()
    px._server.close()  # simulate a crash that leaves the socket file behind
    px._server = None
    assert os.path.exists(sock_path)
    px2 = EgressProxy(sock_path, ALLOW)
    await px2.start()
    try:
        data = await _talk(sock_path, b"GET / HTTP/1.1\r\n\r\n")
        assert data.startswith(b"HTTP/1.1 405")
    finally:
        await px2.stop()


async def test_default_resolver_and_check_refuse_loopback_names():
    # No injection: the real host resolver plus the real address check.
    ips = await egress._default_resolver("localhost")
    assert ips and not any(is_public_ip(ip) for ip in ips)
    assert EgressProxy("/nonexistent", ALLOW)._ip_ok is is_public_ip
