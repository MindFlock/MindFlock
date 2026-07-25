"""Multi-machine control — the async discovery/fetch/connect internals and the
merge across *several* devices at once (:mod:`backend.web.core.remote`).

``test_remote_devices.py`` covers the synchronous surface (title namespacing,
the peer filter, the endpoints, the proxy path split). This file exercises the
parts that only show up with more than one machine on the tailnet and the
best-effort async plumbing that talks to them:

* ``_discover_once`` — never lists ourselves, promotes/demotes ``reachable``
  from the hello probe, keeps a device through a blip, evicts it past the
  stale grace, and refreshes host/os/ip even when the probe fails;
* ``_connected`` — the full reachable × remote_control × auth × token matrix;
* ``_fetch_instances`` / ``connect_device`` — the 401 / 403 / 200 / network-
  error branches, against a fake aiohttp session (no sockets);
* ``merged_instances`` / ``devices_json`` — two devices with *different*
  providers merged into one namespaced snapshot;
* the on-disk token store round-trip.

No network and no tailscale: ``tailscale_nodes`` and ``_probe_peer`` are
monkeypatched, and ``_http_session`` returns an in-memory fake.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.web.core import remote


@pytest.fixture(autouse=True)
def _clean_remote_state(tmp_path, monkeypatch):
    """Isolate each test: no devices, no self, a private tmp token file, auth off.

    Unlike ``test_remote_devices``' fixture we do NOT stub ``_persist_tokens`` —
    ``_tokens_path`` is pointed at a per-test file so the real persistence path
    runs (and is asserted) without ever touching the user's store.
    """
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setattr(remote, "_DEVICES", {})
    monkeypatch.setattr(remote, "_SELF", {})
    monkeypatch.setattr(remote, "_TOKENS", None)  # force a fresh load from tmp
    monkeypatch.setattr(
        remote, "_tokens_path", lambda: str(tmp_path / "remote_devices.json")
    )
    yield


# --------------------------------------------------------------------------- #
# a fake aiohttp session — the async response is an async context manager with
# .status and .json(); network failure is modelled by raising on enter.
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status=200, payload=None, raise_on_enter=None):
        self.status = status
        self._payload = payload
        self._raise = raise_on_enter
        self.headers = {"Content-Type": "application/json"}

    async def __aenter__(self):
        if self._raise is not None:
            raise self._raise
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    """Records requests; replies from a per-call ``handler(method, url, **kw)``."""

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._handler("GET", url, **kw)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self._handler(method, url, **kw)


def _install_session(monkeypatch, handler):
    sess = _FakeSession(handler)

    async def _fake_http_session():
        return sess

    monkeypatch.setattr(remote, "_http_session", _fake_http_session)
    return sess


def _fake_device(**over) -> dict:
    dev = {
        "key": "otherbox",
        "host": "OtherBox",
        "os": "windows",
        "ip": "100.1.2.3",
        "base_url": "http://100.1.2.3:8765",
        "reachable": True,
        "remote_control": True,
        "auth": False,
        "version": "0.1.2",
        "last_seen": 0.0,
        "instances": [],
        "instances_ok": False,
        "error": "",
    }
    dev.update(over)
    return dev


# --------------------------------------------------------------------------- #
# _discover_once — the tailnet sweep lifecycle
# --------------------------------------------------------------------------- #
def _run(coro):
    return asyncio.run(coro)


def test_discovery_never_lists_self(monkeypatch):
    # A peer whose MagicDNS key equals our own identity must never become a
    # controllable device (belt-and-braces against a mislabelled Self).
    self_entry = {
        "key": "mybox",
        "host": "MyBox",
        "dns": "",
        "ip": "100.0.0.1",
        "os": "linux",
        "online": True,
    }
    peer = {
        "key": "mybox",
        "host": "MyBox",
        "dns": "",
        "ip": "100.0.0.9",
        "os": "linux",
        "online": True,
    }
    monkeypatch.setattr(remote, "tailscale_nodes", lambda: (self_entry, [peer]))

    async def _hit(_p):
        return ("http://100.0.0.9:8765", {"app": "mindflock"})

    monkeypatch.setattr(remote, "_probe_peer", _hit)
    _run(remote._discover_once())
    # Even a peer that answers hello is dropped when its key is our own.
    assert remote._DEVICES == {}
    assert remote.self_identity()["key"] == "mybox"


def test_discovery_promotes_reachable_from_hello(monkeypatch):
    self_entry = {
        "key": "mybox",
        "host": "MyBox",
        "dns": "",
        "ip": "100.0.0.1",
        "os": "linux",
        "online": True,
    }
    peer = {
        "key": "otherbox",
        "host": "OtherBox",
        "dns": "otherbox.ts.net",
        "ip": "100.0.0.2",
        "os": "windows",
        "online": True,
    }
    monkeypatch.setattr(remote, "tailscale_nodes", lambda: (self_entry, [peer]))

    async def _hit(_p):
        return (
            "http://100.0.0.2:8765",
            {
                "app": "mindflock",
                "remote_control": True,
                "auth": True,
                "version": "9.9",
            },
        )

    monkeypatch.setattr(remote, "_probe_peer", _hit)
    _run(remote._discover_once())
    dev = remote._DEVICES["otherbox"]
    assert dev["reachable"] is True
    assert dev["remote_control"] is True and dev["auth"] is True
    assert dev["version"] == "9.9"
    assert dev["base_url"] == "http://100.0.0.2:8765"
    assert dev["last_seen"] > 0


def test_discovery_failed_probe_refreshes_metadata_but_not_reachable(monkeypatch):
    # A peer we can see on the tailnet but that never answers hello: metadata
    # (host/os/ip) still tracks, but it stays unreachable with no last_seen — so
    # devices_json keeps hiding it (an online non-MindFlock node is noise).
    self_entry = {
        "key": "mybox",
        "host": "MyBox",
        "dns": "",
        "ip": "100.0.0.1",
        "os": "linux",
        "online": True,
    }
    peer = {
        "key": "winbox",
        "host": "WinBox",
        "dns": "winbox.ts.net",
        "ip": "100.0.0.5",
        "os": "windows",
        "online": True,
    }
    monkeypatch.setattr(remote, "tailscale_nodes", lambda: (self_entry, [peer]))

    async def _miss(_p):
        return None

    monkeypatch.setattr(remote, "_probe_peer", _miss)
    _run(remote._discover_once())
    dev = remote._DEVICES["winbox"]
    assert dev["reachable"] is False
    assert dev["last_seen"] == 0.0
    assert (dev["host"], dev["os"], dev["ip"]) == ("WinBox", "windows", "100.0.0.5")
    # ...and it does not surface as a controllable device.
    assert remote.devices_json()["devices"] == []


def test_discovery_evicts_stale_but_keeps_recent(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(remote.time, "time", lambda: now)
    # Two devices that dropped off the tailnet this sweep (no peers returned):
    # one seen just now (kept through the blip), one seen long ago (evicted).
    remote._DEVICES["fresh"] = _fake_device(
        key="fresh", reachable=True, last_seen=now - 1.0
    )
    remote._DEVICES["stale"] = _fake_device(
        key="stale", reachable=True, last_seen=now - (remote._STALE_AFTER + 5.0)
    )
    monkeypatch.setattr(
        remote,
        "tailscale_nodes",
        lambda: (
            {
                "key": "mybox",
                "host": "MyBox",
                "dns": "",
                "ip": "100.0.0.1",
                "os": "linux",
                "online": True,
            },
            [],
        ),
    )
    _run(remote._discover_once())
    # A sweep that doesn't see a device only evicts it once it's past the stale
    # grace; the recently-seen one is kept (its prior state left untouched — the
    # instances loop, not discovery, demotes reachability on a dropped device).
    assert "fresh" in remote._DEVICES
    assert "stale" not in remote._DEVICES
    assert [d["device"] for d in remote.devices_json()["devices"]] == ["fresh"]


# --------------------------------------------------------------------------- #
# _connected — the full permission/reachability matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "reachable,remote_control,auth,token,expected",
    [
        (True, True, False, "", True),  # no auth needed
        (True, True, True, "tok", True),  # auth + token held
        (True, True, True, "", False),  # auth but no token
        (True, False, False, "", False),  # target toggle off
        (False, True, False, "tok", False),  # unreachable
    ],
)
def test_connected_matrix(reachable, remote_control, auth, token, expected):
    remote._DEVICES["d"] = _fake_device(
        key="d",
        reachable=reachable,
        remote_control=remote_control,
        auth=auth,
    )
    if token:
        remote.set_token("d", token)
    assert remote._connected(remote._DEVICES["d"]) is expected


# --------------------------------------------------------------------------- #
# _fetch_instances — status-code handling into dev state
# --------------------------------------------------------------------------- #
def test_fetch_instances_ok(monkeypatch):
    _install_session(
        monkeypatch,
        lambda m, u, **kw: _FakeResp(200, [{"title": "repo", "status": "running"}]),
    )
    dev = _fake_device()
    _run(remote._fetch_instances(dev))
    assert dev["instances_ok"] is True and dev["error"] == ""
    assert dev["instances"][0]["title"] == "repo"


def test_fetch_instances_401_marks_invalid_token(monkeypatch):
    _install_session(monkeypatch, lambda m, u, **kw: _FakeResp(401, None))
    dev = _fake_device(instances=[{"title": "old"}], instances_ok=True)
    _run(remote._fetch_instances(dev))
    assert dev["instances_ok"] is False
    assert dev["instances"] == []
    assert dev["error"] == "invalid token"


def test_fetch_instances_403_marks_remote_off(monkeypatch):
    _install_session(monkeypatch, lambda m, u, **kw: _FakeResp(403, None))
    dev = _fake_device()
    _run(remote._fetch_instances(dev))
    assert dev["instances_ok"] is False
    assert "remote control is off" in dev["error"]


def test_fetch_instances_network_error_is_swallowed(monkeypatch):
    _install_session(
        monkeypatch,
        lambda m, u, **kw: _FakeResp(raise_on_enter=OSError("connection reset")),
    )
    dev = _fake_device()
    _run(remote._fetch_instances(dev))
    assert dev["instances_ok"] is False
    assert dev["error"]  # some non-empty diagnostic, never raised


def test_fetch_instances_attaches_token_and_remote_header(monkeypatch):
    remote._DEVICES["otherbox"] = _fake_device(auth=True)
    remote.set_token("otherbox", "sekret")
    sess = _install_session(monkeypatch, lambda m, u, **kw: _FakeResp(200, []))
    _run(remote._fetch_instances(remote._DEVICES["otherbox"]))
    _, _, kw = sess.calls[0]
    headers = kw["headers"]
    assert headers["Authorization"] == "Bearer sekret"
    assert remote.REMOTE_HEADER in headers  # so the target applies its gate


# --------------------------------------------------------------------------- #
# connect_device — token validation + persistence
# --------------------------------------------------------------------------- #
def test_connect_unreachable_device():
    remote._DEVICES["otherbox"] = _fake_device(reachable=False)
    ok, err = _run(remote.connect_device("otherbox", "tok"))
    assert ok is False and "not reachable" in err


def test_connect_bad_token_is_rejected_and_not_saved(monkeypatch):
    remote._DEVICES["otherbox"] = _fake_device(auth=True)
    _install_session(monkeypatch, lambda m, u, **kw: _FakeResp(401, None))
    ok, err = _run(remote.connect_device("otherbox", "wrong"))
    assert ok is False and err == "invalid token"
    assert remote.token_for("otherbox") == ""  # never persisted a bad token


def test_connect_success_persists_token_and_seeds_instances(monkeypatch):
    remote._DEVICES["otherbox"] = _fake_device(auth=True)
    _install_session(monkeypatch, lambda m, u, **kw: _FakeResp(200, [{"title": "r"}]))
    ok, err = _run(remote.connect_device("otherbox", "good"))
    assert ok is True and err == ""
    assert remote.token_for("otherbox") == "good"
    assert remote._DEVICES["otherbox"]["instances_ok"] is True
    # And it survived to disk — a fresh load reads it back.
    remote._TOKENS = None
    assert remote.token_for("otherbox") == "good"


# --------------------------------------------------------------------------- #
# merged snapshot across TWO machines with DIFFERENT providers
# --------------------------------------------------------------------------- #
def test_merged_instances_spans_two_devices():
    remote._DEVICES["boxa"] = _fake_device(
        key="boxa",
        host="BoxA",
        instances_ok=True,
        instances=[{"title": "api", "status": "running", "program": "claude"}],
    )
    remote._DEVICES["boxb"] = _fake_device(
        key="boxb",
        host="BoxB",
        instances_ok=True,
        instances=[{"title": "web", "status": "idle", "program": "codex"}],
    )
    merged = remote.merged_instances()
    by_title = {m["title"]: m for m in merged}
    assert set(by_title) == {"boxa::api", "boxb::web"}
    # Each entry keeps its own device label and its provider passes through
    # untouched — the sidebar shows a claude session on one box and a codex
    # session on the other.
    assert by_title["boxa::api"]["device_label"] == "BoxA"
    assert by_title["boxa::api"]["program"] == "claude"
    assert by_title["boxb::web"]["device_label"] == "BoxB"
    assert by_title["boxb::web"]["program"] == "codex"


def test_merged_instances_skips_only_the_disconnected_device():
    # One connected, one unreachable — only the connected device's sessions
    # merge; the other is silently dropped (not an error).
    remote._DEVICES["up"] = _fake_device(
        key="up", instances_ok=True, instances=[{"title": "t"}]
    )
    remote._DEVICES["down"] = _fake_device(
        key="down", reachable=False, instances_ok=True, instances=[{"title": "z"}]
    )
    titles = [m["title"] for m in remote.merged_instances()]
    assert titles == ["up::t"]


def test_devices_json_counts_sessions_per_device():
    remote._DEVICES["boxa"] = _fake_device(
        key="boxa", instances_ok=True, instances=[{"title": "a"}, {"title": "b"}]
    )
    remote._DEVICES["boxb"] = _fake_device(
        key="boxb", auth=True, instances_ok=False  # needs a token -> not connected
    )
    devs = {d["device"]: d for d in remote.devices_json()["devices"]}
    assert devs["boxa"]["sessions"] == 2
    assert devs["boxa"]["connected"] is True
    assert devs["boxb"]["sessions"] == 0
    assert devs["boxb"]["needs_token"] is True
    assert devs["boxb"]["connected"] is False


# --------------------------------------------------------------------------- #
# candidate probe URLs + token store round-trip
# --------------------------------------------------------------------------- #
def test_candidate_bases_order(monkeypatch):
    monkeypatch.setattr(remote, "_SERVER_PORT", 9000)
    bases = remote._candidate_bases({"ip": "100.0.0.2", "dns": "otherbox.ts.net"})
    # This server's own port first (fleets share config), then the default web
    # port, then HTTPS via MagicDNS (a peer behind `tailscale serve`).
    assert bases == [
        "http://100.0.0.2:9000",
        "http://100.0.0.2:8765",
        "https://otherbox.ts.net",
    ]


def test_candidate_bases_dedupes_default_port(monkeypatch):
    monkeypatch.setattr(remote, "_SERVER_PORT", 8765)
    bases = remote._candidate_bases({"ip": "100.0.0.2", "dns": ""})
    assert bases == ["http://100.0.0.2:8765"]  # no duplicate 8765, no dns entry


def test_forget_device_clears_token_and_sessions():
    remote._DEVICES["otherbox"] = _fake_device(
        auth=True, instances_ok=True, instances=[{"title": "t"}], error="x"
    )
    remote.set_token("otherbox", "tok")
    remote.forget_device("otherbox")
    assert remote.token_for("otherbox") == ""
    dev = remote._DEVICES["otherbox"]
    assert dev["instances"] == [] and dev["instances_ok"] is False
    # A reload from disk confirms the token is gone for good.
    remote._TOKENS = None
    assert remote.token_for("otherbox") == ""
