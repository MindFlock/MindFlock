"""Tailscale mode binds loopback + this node's Tailscale addresses, not the LAN
(:mod:`backend.web.core.tailnet_bind`, ``run.main``), with the 0.0.0.0
fallbacks and the rebind once tailscaled comes up."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import types

import pytest

from backend.web.core import restart, tailnet_bind


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(tailnet_bind.BIND_ALL_ENV, raising=False)
    monkeypatch.setenv(tailnet_bind._REBINDS_ENV, "")


def _status(monkeypatch, payload, rc=0):
    monkeypatch.setattr(tailnet_bind.shutil, "which", lambda n: "/usr/bin/tailscale")

    def _run(cmd, **kw):
        assert cmd[1:] == ["status", "--json"]
        return types.SimpleNamespace(returncode=rc, stdout=json.dumps(payload).encode())

    monkeypatch.setattr(tailnet_bind.subprocess, "run", _run)


# --------------------------------------------------------------------------- #
# tailnet_ips — the conftest stubs it out; these restore the real one.
# --------------------------------------------------------------------------- #
_REAL_IPS = tailnet_bind.tailnet_ips  # captured at import, before any fixture


@pytest.fixture()
def real_ips():
    return _REAL_IPS


def test_ips_ipv4_first_while_running(monkeypatch, real_ips):
    _status(
        monkeypatch,
        {
            "BackendState": "Running",
            "Self": {"TailscaleIPs": ["fd7a:115c:a1e0::1", "100.64.0.7", "junk"]},
        },
    )
    assert real_ips() == ["100.64.0.7", "fd7a:115c:a1e0::1"]


@pytest.mark.parametrize("state", ["Stopped", "NeedsLogin", "Starting", ""])
def test_no_ips_unless_running(monkeypatch, real_ips, state):
    _status(
        monkeypatch, {"BackendState": state, "Self": {"TailscaleIPs": ["100.64.0.7"]}}
    )
    assert real_ips() == []


def test_no_ips_without_the_cli_or_on_error(monkeypatch, real_ips):
    monkeypatch.setattr(tailnet_bind.shutil, "which", lambda n: None)
    assert real_ips() == []
    _status(monkeypatch, {}, rc=1)
    assert real_ips() == []

    def _boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(tailnet_bind.subprocess, "run", _boom)
    assert real_ips() == []


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_local_mode_is_loopback_only(monkeypatch):
    monkeypatch.setattr(tailnet_bind, "tailnet_ips", lambda: ["100.64.0.7"])
    assert tailnet_bind.plan("local") == (["127.0.0.1"], "")


def test_tailscale_mode_binds_loopback_and_the_tailnet(monkeypatch):
    monkeypatch.setattr(
        tailnet_bind, "tailnet_ips", lambda: ["100.64.0.7", "fd7a:115c:a1e0::1"]
    )
    monkeypatch.setattr(tailnet_bind, "bindable", lambda h: True)
    hosts, why = tailnet_bind.plan("tailscale")
    assert hosts == ["127.0.0.1", "100.64.0.7", "fd7a:115c:a1e0::1"]
    assert why == tailnet_bind.WHY_TAILNET
    assert "0.0.0.0" not in hosts


def test_only_the_bindable_addresses(monkeypatch):
    monkeypatch.setattr(
        tailnet_bind, "tailnet_ips", lambda: ["100.64.0.7", "fd7a:115c:a1e0::1"]
    )
    monkeypatch.setattr(tailnet_bind, "bindable", lambda h: ":" not in h)
    assert tailnet_bind.plan("tailscale")[0] == ["127.0.0.1", "100.64.0.7"]


def test_falls_back_to_all_without_a_tailnet(monkeypatch):
    monkeypatch.setattr(tailnet_bind, "tailnet_ips", lambda: [])
    assert tailnet_bind.plan("tailscale") == (["0.0.0.0"], tailnet_bind.WHY_NO_TAILNET)


def test_falls_back_to_all_when_nothing_binds(monkeypatch):
    """Userspace networking, or WSL reading Windows' tailscale.exe."""
    monkeypatch.setattr(tailnet_bind, "tailnet_ips", lambda: ["100.64.0.7"])
    monkeypatch.setattr(tailnet_bind, "bindable", lambda h: False)
    assert tailnet_bind.plan("tailscale") == (
        ["0.0.0.0"],
        tailnet_bind.WHY_UNBINDABLE,
    )


def test_bind_all_on_request(monkeypatch):
    monkeypatch.setenv(tailnet_bind.BIND_ALL_ENV, "1")
    monkeypatch.setattr(tailnet_bind, "tailnet_ips", lambda: ["100.64.0.7"])
    assert tailnet_bind.plan("tailscale") == (["0.0.0.0"], tailnet_bind.WHY_ALL)


def test_bindable_is_a_real_probe():
    assert tailnet_bind.bindable("127.0.0.1") is True
    assert tailnet_bind.bindable("192.0.2.1") is False  # TEST-NET, not ours


# --------------------------------------------------------------------------- #
# open_sockets
# --------------------------------------------------------------------------- #
def test_open_sockets_binds_each_host():
    socks = tailnet_bind.open_sockets(["127.0.0.1"], 0)
    try:
        assert [s.getsockname()[0] for s in socks] == ["127.0.0.1"]
    finally:
        for s in socks:
            s.close()


def test_open_sockets_closes_what_it_opened_on_failure(monkeypatch):
    opened = []
    real = socket.socket

    def _track(*a, **kw):
        s = real(*a, **kw)
        opened.append(s)
        return s

    monkeypatch.setattr(tailnet_bind.socket, "socket", _track)
    with pytest.raises(OSError):
        tailnet_bind.open_sockets(["127.0.0.1", "192.0.2.1"], 0)
    assert len(opened) == 2 and all(s.fileno() == -1 for s in opened)


# --------------------------------------------------------------------------- #
# run.main
# --------------------------------------------------------------------------- #
@pytest.fixture()
def run_mod(monkeypatch, isolate_settings_store):
    from backend import doctor
    from backend.doctor import Check
    from backend.web import run as run_mod

    monkeypatch.setenv("CS_WEB_MODE", "")
    monkeypatch.setenv("UVICORN_PORT", "8765")
    monkeypatch.setattr(run_mod, "_port_squatter", lambda host, port: "")
    monkeypatch.setattr(run_mod, "_is_onboarded", lambda: True)
    _ok = Check(id="stub", label="stub", status="ok")
    for _name in ("check_git", "check_tmux", "check_agent_cli"):
        monkeypatch.setattr(doctor, _name, lambda: _ok)
    return run_mod


def test_run_tailscale_serves_on_loopback_plus_tailnet(monkeypatch, run_mod):
    import uvicorn

    monkeypatch.setattr(tailnet_bind, "tailnet_ips", lambda: ["100.64.0.7"])
    monkeypatch.setattr(tailnet_bind, "bindable", lambda h: True)
    opened = {}
    monkeypatch.setattr(
        tailnet_bind,
        "open_sockets",
        lambda hosts, port: opened.update(hosts=hosts, port=port) or ["s1", "s2"],
    )
    served = []
    monkeypatch.setattr(
        run_mod, "_serve_sockets", lambda app, socks: served.append(socks)
    )
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("single bind"))
    run_mod.main(["tailscale"])
    assert opened == {"hosts": ["127.0.0.1", "100.64.0.7"], "port": 8765}
    assert served == [["s1", "s2"]]
    assert not tailnet_bind.fell_back()


def test_run_tailscale_without_tailnet_falls_back_and_arms_rebind(monkeypatch, run_mod):
    import os

    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    run_mod.main(["tailscale"])
    assert seen["host"] == "0.0.0.0"
    assert os.environ.get(tailnet_bind.FALLBACK_ENV) == "1"
    assert tailnet_bind.fell_back() is True


def test_run_all_binds_every_interface(monkeypatch, run_mod):
    import uvicorn

    monkeypatch.setattr(tailnet_bind, "tailnet_ips", lambda: ["100.64.0.7"])
    monkeypatch.setattr(tailnet_bind, "bindable", lambda h: True)
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    run_mod.main(["all"])
    assert seen["host"] == "0.0.0.0"


# --------------------------------------------------------------------------- #
# rebind_loop
# --------------------------------------------------------------------------- #
@pytest.fixture()
def reexecs(monkeypatch):
    calls = []
    monkeypatch.setattr(restart, "is_serving", lambda: True)
    monkeypatch.setattr(restart, "reexec_soon", lambda **kw: calls.append(kw))
    monkeypatch.setenv(tailnet_bind.FALLBACK_ENV, "1")
    return calls


def test_rebinds_once_tailscale_is_up(monkeypatch, reexecs):
    ups = iter([[], ["100.64.0.7"]])
    monkeypatch.setattr(tailnet_bind, "tailnet_ips", lambda: next(ups))
    monkeypatch.setattr(tailnet_bind, "bindable", lambda h: True)
    asyncio.run(asyncio.wait_for(tailnet_bind.rebind_loop(interval=0), 5))
    assert reexecs == [{"delay": 0.5, "keep_mode": True}]


def test_no_rebind_while_the_addresses_are_not_ours(monkeypatch, reexecs):
    monkeypatch.setattr(tailnet_bind, "tailnet_ips", lambda: ["100.64.0.7"])
    monkeypatch.setattr(tailnet_bind, "bindable", lambda h: False)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(tailnet_bind.rebind_loop(interval=0.01), 0.2))
    assert reexecs == []


def test_rebinds_are_capped(monkeypatch, reexecs):
    monkeypatch.setenv(tailnet_bind._REBINDS_ENV, str(tailnet_bind.MAX_REBINDS))
    monkeypatch.setattr(tailnet_bind, "tailnet_ips", lambda: ["100.64.0.7"])
    monkeypatch.setattr(tailnet_bind, "bindable", lambda h: True)
    asyncio.run(asyncio.wait_for(tailnet_bind.rebind_loop(interval=0), 5))
    assert reexecs == []


def test_rebind_loop_quits_when_not_fallen_back(monkeypatch, reexecs):
    monkeypatch.setenv(tailnet_bind.FALLBACK_ENV, "")
    asyncio.run(asyncio.wait_for(tailnet_bind.rebind_loop(interval=0), 5))
    assert reexecs == []
