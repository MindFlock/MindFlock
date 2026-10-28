"""The guest links' public address (:mod:`backend.web.core.guest_tunnel`):
one cloudflared quick tunnel to this server, started in the background,
its hostname let through ``host_ok`` while up and forgotten when it stops.
A fake tunnel object stands in for cloudflared.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.web.core import auth, guest_tunnel


class FakeTunnel:
    def __init__(self, host="brave-owl-42.trycloudflare.com", fail=None, delay=0.0):
        self.host, self.fail, self.delay = host, fail, delay
        self.stopped = 0
        self.hostname = None

    async def start(self):
        await asyncio.sleep(self.delay)
        if self.fail is not None:
            from backend.peer.tunnel import TunnelError

            raise TunnelError("nope", self.fail)
        self.hostname = self.host
        return self.host

    async def stop(self):
        self.stopped += 1
        self.hostname = None


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for k, v in {
        "_TUNNEL": None,
        "_TASK": None,
        "_HOST": "",
        "_ERROR": "",
        "_REASON": "",
        "_WANTED": False,
    }.items():
        monkeypatch.setattr(guest_tunnel, k, v)
    monkeypatch.setattr(guest_tunnel, "_binary", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(auth, "_FRONTED_HOSTS", set())
    yield


def test_status_off_by_default():
    assert guest_tunnel.status() == {
        "state": "off",
        "url": "",
        "installed": True,
        "error": "",
        "reason": "",
    }


def test_ensure_starts_in_the_background_and_fronts_the_host(monkeypatch):
    fake = FakeTunnel(delay=0.01)
    monkeypatch.setattr(guest_tunnel, "_make_tunnel", lambda binary: fake)

    async def run():
        st = guest_tunnel.ensure()
        assert st["state"] == "starting" and st["url"] == ""
        await asyncio.sleep(0.05)
        st = guest_tunnel.status()
        assert (
            st["state"] == "up"
            and st["url"] == "https://brave-owl-42.trycloudflare.com/"
        )
        assert "brave-owl-42.trycloudflare.com" in auth._FRONTED_HOSTS
        # A second ensure while up is a no-op.
        assert guest_tunnel.ensure()["state"] == "up"
        await guest_tunnel.stop()
        assert fake.stopped == 1
        assert guest_tunnel.status()["state"] == "off"
        assert "brave-owl-42.trycloudflare.com" not in auth._FRONTED_HOSTS

    asyncio.run(run())


def test_failure_is_reported_in_words(monkeypatch):
    monkeypatch.setattr(
        guest_tunnel, "_make_tunnel", lambda binary: FakeTunnel(fail="rate_limited")
    )

    async def run():
        guest_tunnel.ensure()
        await asyncio.sleep(0.02)
        st = guest_tunnel.status()
        assert st["state"] == "failed" and st["reason"] == "rate_limited"
        assert "rate-limit" in st["error"]

    asyncio.run(run())


def test_no_binary_is_not_installed(monkeypatch):
    monkeypatch.setattr(guest_tunnel, "_binary", lambda: None)

    async def run():
        st = guest_tunnel.ensure()
        await asyncio.sleep(0.02)
        st = guest_tunnel.status()
        assert st["installed"] is False and st["state"] == "failed"
        assert st["reason"] == "not_installed"
        assert guest_tunnel.wanted() is True  # the doctor now offers the install

    asyncio.run(run())


def test_exit_clears_the_host_and_the_front(monkeypatch):
    fake = FakeTunnel()
    monkeypatch.setattr(guest_tunnel, "_make_tunnel", lambda binary: fake)

    async def run():
        guest_tunnel.ensure()
        await asyncio.sleep(0.02)
        assert guest_tunnel.status()["state"] == "up"
        guest_tunnel._exited()
        st = guest_tunnel.status()
        assert st["state"] == "failed" and st["reason"] == "exited"
        assert not auth._FRONTED_HOSTS

    asyncio.run(run())


def test_host_ok_passes_the_tunnel_name_in_every_mode(monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    auth.allow_fronted_host("brave-owl-42.trycloudflare.com")
    scope = {"headers": [(b"host", b"brave-owl-42.trycloudflare.com")]}
    assert auth.host_ok(scope) is True
    other = {"headers": [(b"host", b"evil.trycloudflare.com")]}
    assert auth.host_ok(other) is False
