"""Tailnet MSS clamp (:mod:`backend.web.core.tailnet_mtu`): a tunnel MTU with
no room for Tailscale's headers on the underlay blackholes large responses,
so the tailnet listeners (and this server's own outgoing tailnet connections)
cap their TCP segment size — never loopback — and the doctor says why."""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import pytest

from backend import doctor
from backend.web.core import tailnet_bind, tailnet_mtu
from backend.web.core.tailnet_mtu import Plan

# The conftest stubs ``detect`` (no test sees the machine's MTUs); captured at
# import, before any fixture, so the fake-tree tests run the real one.
_REAL_DETECT = tailnet_mtu.detect

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(socket, "TCP_MAXSEG"),
    reason="the clamp is Linux-only",
)

_WSL = Plan("tailscale0", 1280, "eth0", 1280, 1160)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(tailnet_mtu.ENV, raising=False)


@pytest.fixture()
def linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")


def _tree(
    tmp_path: Path,
    mtus: dict,
    route: str = "",
    if_inet6: str = "",
    ipv6_route: str = "",
) -> dict:
    """A fake ``/sys`` + ``/proc`` under ``tmp_path``; returns ``detect`` roots."""
    for name, mtu in mtus.items():
        d = tmp_path / "sys/class/net" / name
        d.mkdir(parents=True)
        (d / "mtu").write_text("%d\n" % mtu)
    net = tmp_path / "proc/net"
    net.mkdir(parents=True)
    header = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    (net / "route").write_text(header + route)
    (net / "if_inet6").write_text(if_inet6)
    (net / "ipv6_route").write_text(ipv6_route)
    return {"sys_root": str(tmp_path), "proc_root": str(tmp_path)}


#: The owner's WSL2 laptop, as read on 2026-10-14.
_WSL_ROUTE = (
    "eth0\t00000000\t015011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t005011AC\t00000000\t0001\t0\t0\t0\t00F0FFFF\t0\t0\t0\n"
)
_WSL_INET6 = (
    "00000000000000000000000000000001 01 80 10 80       lo\n"
    "fd7a115ca1e0000000000000dd3b2437 03 80 00 80 tailscale0\n"
    "fe8000000000000002155dfffe81d53a 02 40 20 80     eth0\n"
)
_NO_V4 = lambda name: ""  # noqa: E731


# --------------------------------------------------------------------------- #
# The math
# --------------------------------------------------------------------------- #
def test_wsl_numbers_clamp_to_1160():
    assert tailnet_mtu.safe_mss(1280, 1280) == 1160


def test_no_clamp_when_the_underlay_has_room():
    assert tailnet_mtu.safe_mss(1280, 1500) == 0
    assert tailnet_mtu.safe_mss(1280, 1360) == 0  # exactly fits
    assert tailnet_mtu.safe_mss(1280, 1359) == 1239  # 1359 - 80 - 40


def test_floor_and_unknowns():
    assert tailnet_mtu.safe_mss(1280, 576) == tailnet_mtu.MIN_MSS
    assert tailnet_mtu.safe_mss(0, 1280) == 0
    assert tailnet_mtu.safe_mss(1280, 0) == 0


def test_ipv6_sockets_get_20_bytes_less():
    assert _WSL.mss_for(socket.AF_INET) == 1160
    assert _WSL.mss_for(socket.AF_INET6) == 1140
    forced = Plan(mss=1000, source="env")
    assert forced.mss_for(socket.AF_INET6) == 1000
    assert Plan().mss_for(socket.AF_INET6) == 0


def test_tailnet_addresses():
    for a in ("100.64.0.7", "100.127.255.1", "fd7a:115c:a1e0::dd3b:2437"):
        assert tailnet_mtu.is_tailnet_addr(a)
    for a in ("127.0.0.1", "::1", "0.0.0.0", "192.168.1.5", "100.128.0.1", "x"):
        assert not tailnet_mtu.is_tailnet_addr(a)


# --------------------------------------------------------------------------- #
# Discovery from /sys + /proc
# --------------------------------------------------------------------------- #
def test_detects_the_wsl_laptop(tmp_path, linux):
    roots = _tree(
        tmp_path,
        {"lo": 65536, "eth0": 1280, "tailscale0": 1280},
        _WSL_ROUTE,
        _WSL_INET6,
    )
    assert _REAL_DETECT(**roots, ipv4_of=_NO_V4) == _WSL


def test_no_clamp_on_a_1500_underlay(tmp_path, linux):
    roots = _tree(tmp_path, {"eth0": 1500, "tailscale0": 1280}, _WSL_ROUTE)
    p = _REAL_DETECT(**roots, ipv4_of=_NO_V4)
    assert (p.tunnel_mtu, p.underlay_mtu, p.mss, p.mismatch) == (1280, 1500, 0, False)


def test_renamed_tunnel_found_by_its_ipv6_address(tmp_path, linux):
    inet6 = _WSL_INET6.replace("tailscale0", "ts-tun")
    roots = _tree(
        tmp_path, {"lo": 65536, "eth0": 1280, "ts-tun": 1280}, _WSL_ROUTE, inet6
    )
    assert _REAL_DETECT(**roots, ipv4_of=_NO_V4).tunnel_if == "ts-tun"


def test_renamed_tunnel_found_by_its_ipv4_address(tmp_path, linux):
    roots = _tree(tmp_path, {"lo": 65536, "eth0": 1280, "wg9": 1280}, _WSL_ROUTE)
    v4 = {"eth0": "172.17.80.5", "wg9": "100.101.102.103"}.get
    p = _REAL_DETECT(**roots, ipv4_of=lambda n: v4(n) or "")
    assert (p.tunnel_if, p.underlay_if, p.mss) == ("wg9", "eth0", 1160)


def test_no_tunnel_means_no_plan(tmp_path, linux):
    # WSL with Tailscale only on the Windows side: nothing local to clamp.
    roots = _tree(tmp_path, {"lo": 65536, "eth0": 1280}, _WSL_ROUTE)
    assert _REAL_DETECT(**roots, ipv4_of=_NO_V4) == Plan()


def test_underlay_is_the_lowest_metric_default_route_not_the_tunnel(tmp_path):
    route = (
        "tailscale0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"
        "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        "eth1\t0000A8C0\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    )
    roots = _tree(tmp_path, {}, route)
    got = tailnet_mtu.underlay_interface(roots["proc_root"], exclude="tailscale0")
    assert got == "eth0"


def test_underlay_falls_back_to_the_ipv6_default_route(tmp_path):
    z = "0" * 32
    ipv6 = (
        f"{z} 00 {z} 00 {z} ffffffff 00000001 00000000 00200200       lo\n"
        f"{z} 00 {z} 00 fe800000000000000000000000000001 00000400 00000001 00000000 00450003 eth0\n"
    )
    roots = _tree(tmp_path, {}, "", "", ipv6)
    assert tailnet_mtu.underlay_interface(roots["proc_root"]) == "eth0"


def test_detect_is_linux_only(tmp_path, monkeypatch):
    roots = _tree(tmp_path, {"eth0": 1280, "tailscale0": 1280}, _WSL_ROUTE)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _REAL_DETECT(**roots, ipv4_of=_NO_V4) == Plan()


def test_detect_never_raises_on_a_missing_tree(tmp_path, linux):
    assert (
        _REAL_DETECT(sys_root=str(tmp_path / "nope"), proc_root=str(tmp_path)) == Plan()
    )


# --------------------------------------------------------------------------- #
# MINDFLOCK_TAILNET_MSS
# --------------------------------------------------------------------------- #
@pytest.fixture()
def wsl_detect(monkeypatch, linux):
    monkeypatch.setattr(tailnet_mtu, "detect", lambda **_kw: _WSL)


def test_env_unset_is_auto(wsl_detect):
    assert tailnet_mtu.plan({}) == _WSL


@pytest.mark.parametrize("raw", ["0", "off", "false"])
def test_env_disables(wsl_detect, raw):
    p = tailnet_mtu.plan({tailnet_mtu.ENV: raw})
    assert (p.mss, p.source, p.mismatch) == (0, "off", True)


def test_env_forces_a_value(wsl_detect):
    p = tailnet_mtu.plan({tailnet_mtu.ENV: "1000"})
    assert (p.mss, p.source) == (1000, "env")


def test_env_forces_even_without_a_mismatch(monkeypatch, linux):
    monkeypatch.setattr(tailnet_mtu, "detect", lambda **_kw: Plan("tailscale0", 1280))
    assert tailnet_mtu.plan({tailnet_mtu.ENV: "1200"}).mss == 1200


@pytest.mark.parametrize("raw", ["junk", "50", "70000", "-5"])
def test_env_nonsense_is_auto(wsl_detect, raw):
    assert tailnet_mtu.plan({tailnet_mtu.ENV: raw}) == _WSL


# --------------------------------------------------------------------------- #
# Real sockets
# --------------------------------------------------------------------------- #
def _maxseg(s) -> int:
    return s.getsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG)


@linux_only
def test_a_clamped_listener_caps_accepted_connections():
    """What the whole fix rests on: TCP_MAXSEG set on the listening socket
    before listen() reaches every connection it accepts (and the peer, via the
    MSS we advertise). Loopback's 64K MTU would otherwise allow ~32K."""

    def _pair(mss: int):
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        if mss:
            assert tailnet_mtu.apply(ls, mss)
        ls.listen()
        c = socket.create_connection(ls.getsockname())
        a, _ = ls.accept()
        return ls, c, a

    ls, c, a = _pair(1160)
    try:
        assert _maxseg(a) <= 1160 and _maxseg(c) <= 1160
    finally:
        for s in (ls, c, a):
            s.close()
    ls, c, a = _pair(0)
    try:
        assert _maxseg(a) > 1160
    finally:
        for s in (ls, c, a):
            s.close()


class _FakeSock:
    """A bound socket as far as clamp_listeners can tell."""

    def __init__(self, host: str):
        self.host = host
        self.family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.opts = []

    def getsockname(self):
        return (self.host, 8765)

    def setsockopt(self, level, opt, value):
        self.opts.append((level, opt, value))


@linux_only
def test_clamp_listeners_clamps_tailnet_sockets_only():
    v4, v6 = _FakeSock("100.64.0.7"), _FakeSock("fd7a:115c:a1e0::dd3b:2437")
    (loop,) = tailnet_bind.open_sockets(["127.0.0.1"], 0)
    try:
        before = _maxseg(loop)
        line = tailnet_mtu.clamp_listeners([loop, v4, v6], _WSL)
        assert _maxseg(loop) == before  # loopback never clamped
    finally:
        loop.close()
    assert v4.opts == [(socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1160)]
    assert v6.opts == [(socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1140)]
    assert line == (
        "tailnet MTU: tunnel 1280 over underlay 1280 — clamping TCP MSS to 1160 "
        "so large responses reach other devices"
    )


@linux_only
def test_clamp_listeners_is_silent_with_nothing_to_do():
    (loop,) = tailnet_bind.open_sockets(["127.0.0.1"], 0)
    try:
        assert tailnet_mtu.clamp_listeners([loop], _WSL) == ""  # loopback only
    finally:
        loop.close()
    v4 = _FakeSock("100.64.0.7")
    assert tailnet_mtu.clamp_listeners([v4], Plan()) == ""  # no mismatch
    assert (
        tailnet_mtu.clamp_listeners(
            [v4], Plan("tailscale0", 1280, "eth0", 1280, 0, "off")
        )
        == ""
    )
    assert v4.opts == []
    # Not sockets at all (a test stub): skipped, never raised.
    assert tailnet_mtu.clamp_listeners(["s1", object()], _WSL) == ""


@linux_only
def test_clamp_listeners_names_a_forced_value():
    line = tailnet_mtu.clamp_listeners(
        [_FakeSock("100.64.0.7")], Plan(mss=1000, source="env")
    )
    assert line == "tailnet MTU: clamping TCP MSS to 1000 (MINDFLOCK_TAILNET_MSS)"


def test_run_prints_the_clamp_and_hands_uvicorn_the_clamped_sockets(
    monkeypatch, capsys, isolate_settings_store
):
    from backend import doctor as _doctor
    from backend.doctor import Check
    from backend.web import run as run_mod

    import uvicorn

    monkeypatch.setenv("CS_WEB_MODE", "")
    monkeypatch.setenv("UVICORN_PORT", "8765")
    # setenv, not delenv: run.main writes os.environ itself (the `all` mode
    # word), and a delenv of an absent key restores nothing afterwards.
    monkeypatch.setenv(tailnet_bind.BIND_ALL_ENV, "")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("single bind"))
    monkeypatch.setattr(run_mod, "_port_squatter", lambda host, port: "")
    monkeypatch.setattr(run_mod, "_is_onboarded", lambda: True)
    _ok = Check(id="stub", label="stub", status="ok")
    for _name in ("check_git", "check_tmux", "check_agent_cli"):
        monkeypatch.setattr(_doctor, _name, lambda: _ok)
    monkeypatch.setattr(tailnet_bind, "tailnet_ips", lambda: ["100.64.0.7"])
    monkeypatch.setattr(tailnet_bind, "bindable", lambda h: True)
    socks = [_FakeSock("127.0.0.1"), _FakeSock("100.64.0.7")]
    monkeypatch.setattr(tailnet_bind, "open_sockets", lambda hosts, port: socks)
    monkeypatch.setattr(tailnet_mtu, "_CURRENT", _WSL)
    served = []
    monkeypatch.setattr(run_mod, "_serve_sockets", lambda app, s: served.append(s))
    run_mod.main(["tailscale"])
    assert served == [socks]
    assert socks[0].opts == [] and socks[1].opts[0][2] == 1160
    assert "clamping TCP MSS to 1160" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Outgoing: remote.py's shared aiohttp session
# --------------------------------------------------------------------------- #
@linux_only
def test_socket_factory_clamps_tailnet_destinations(monkeypatch):
    monkeypatch.setattr(tailnet_mtu, "_CURRENT", _WSL)
    tcp = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "")
    with tailnet_mtu.socket_factory(tcp + (("100.64.0.7", 8765),)) as s:
        assert _maxseg(s) == 1160
    with tailnet_mtu.socket_factory(tcp + (("192.168.1.5", 8765),)) as s:
        assert _maxseg(s) != 1160
    tcp6 = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "")
    with tailnet_mtu.socket_factory(tcp6 + (("fd7a:115c:a1e0::1", 8765, 0, 0),)) as s:
        assert _maxseg(s) == 1140


def test_client_connector_only_when_clamping(monkeypatch):
    aiohttp = pytest.importorskip("aiohttp")
    monkeypatch.setattr(tailnet_mtu, "_CURRENT", Plan())
    assert tailnet_mtu.client_connector() is None

    async def _make():
        conn = tailnet_mtu.client_connector()
        try:
            return type(conn), getattr(conn, "_socket_factory", None)
        finally:
            await conn.close()

    monkeypatch.setattr(tailnet_mtu, "_CURRENT", _WSL)
    kind, factory = asyncio.run(_make())
    assert kind is aiohttp.TCPConnector
    assert factory is tailnet_mtu.socket_factory


def test_remote_session_uses_the_clamping_connector(monkeypatch):
    aiohttp = pytest.importorskip("aiohttp")
    from backend.web.core import remote

    sentinel = object()
    seen = {}

    class _Session:
        closed = False

        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(tailnet_mtu, "client_connector", lambda: sentinel)
    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(remote, "_HTTP", None)
    asyncio.run(remote._http_session())
    assert seen == {"connector": sentinel}


# --------------------------------------------------------------------------- #
# mindflock doctor
# --------------------------------------------------------------------------- #
def test_doctor_registers_the_check():
    assert doctor.CHECKS_BY_ID["tailnet-mtu"] is doctor.check_tailnet_mtu


def test_doctor_omits_it_without_a_tunnel(monkeypatch):
    monkeypatch.setattr(tailnet_mtu, "plan", lambda: Plan())
    assert doctor.check_tailnet_mtu() is None


def test_doctor_ok_when_it_fits(monkeypatch):
    monkeypatch.setattr(
        tailnet_mtu, "plan", lambda: Plan("tailscale0", 1280, "eth0", 1500)
    )
    c = doctor.check_tailnet_mtu()
    assert (c.id, c.status) == ("tailnet-mtu", "ok")
    assert c.detail == "tailscale0 1280 fits inside eth0 1500"


def test_doctor_warns_with_both_mtus_and_the_fix(monkeypatch):
    monkeypatch.setattr(tailnet_mtu, "plan", lambda: _WSL)
    c = doctor.check_tailnet_mtu()
    assert (c.id, c.status) == ("tailnet-mtu", "warn")
    assert "tailscale0 MTU 1280 over eth0 MTU 1280" in c.detail
    assert "MindFlock caps its own tailnet connections (TCP MSS 1160)" in c.detail
    assert "sudo ip link set dev tailscale0 mtu 1200" in c.fix
    assert "TS_DEBUG_MTU=1200 to /etc/default/tailscaled" in c.fix
    assert "restart tailscaled" in c.fix
    assert not c.install and not c.cmd  # a system network change: never auto-run


def test_doctor_says_when_the_clamp_is_off(monkeypatch):
    off = Plan("tailscale0", 1280, "eth0", 1280, 0, "off")
    monkeypatch.setattr(tailnet_mtu, "plan", lambda: off)
    c = doctor.check_tailnet_mtu()
    assert c.status == "warn"
    assert "clamp is off (MINDFLOCK_TAILNET_MSS=0)" in c.detail


def test_doctor_row_is_an_optional_extra():
    from backend.web.core import setup_steps

    assert "tailnet-mtu" in setup_steps.EXTRA_CHECKS
