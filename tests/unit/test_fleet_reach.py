"""Joining makes you reachable, says why when it can't, and a join can be
approved from wherever you are.

* reachability: ``create_invite`` on a local-only device warns
  (``local_only``), ``status`` says ``self_reachable`` / ``listening`` /
  ``gate_on``, the joiner's join status says whether it is reachable, and
  the admitter re-probes the device it let in (``unreachable_joiner``);
* discovery reasons: what a probe found (refused / timeout / tls / http /
  asleep), per tailnet peer and on member rows;
* one "Paste a code" router (:func:`fleet.classify_code`);
* approve from any member: a request waiting on one member is copied to the
  others (``/api/fleet/pending``, fleet key) and answered there
  (``/api/fleet/member-approve``, fleet key + the 6-digit code);
* "Match my other devices" and the phone-link host table.

Multi-device flows reuse :mod:`tests.unit.test_fleet`'s in-process world.
"""

from __future__ import annotations

import asyncio
import errno
import ssl

import pytest

from backend.web.core import fleet, remote
from tests.unit.test_fleet import (  # noqa: F401 — fixtures
    _clean,
    _join_by_code,
    _wait_join,
    _World,
    events,
    world,
)

try:
    import aiohttp
except Exception:  # noqa: BLE001
    aiohttp = None


# --------------------------------------------------------------------------- #
# rank 1: reachable or local-only
# --------------------------------------------------------------------------- #
def test_create_invite_on_a_local_only_device_warns(monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "local")
    inv = fleet.create_invite()
    assert inv["warning"] == "local_only"
    assert len(inv["code"]) == 9  # the code is still made
    st = fleet.status(True)
    assert st["self_reachable"] is False and st["listening"] == "local"


def test_create_invite_on_a_tailnet_device_has_no_warning(monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    assert "warning" not in fleet.create_invite()
    st = fleet.status(True)
    assert st["self_reachable"] is True and st["listening"] == "tailnet"


def test_unknown_mode_counts_as_reachable(monkeypatch):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    assert fleet.listening() == "" and fleet.self_reachable() is True
    assert "warning" not in fleet.create_invite()


def test_status_says_whether_the_gate_is_on(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH", "1")
    assert fleet.status(True)["gate_on"] is True
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    assert fleet.status(True)["gate_on"] is False


async def test_a_local_only_joiner_is_told_and_the_admitter_sees_it(world, monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "local")
    status, out = await _join_by_code(world, "rig", "laptop")
    assert status == 200 and out["state"] == "joined"
    # The joiner: joined, but none of the others can reach it.
    assert out["self_reachable"] is False
    with world.on("laptop"):
        [rec] = fleet.admitted()
        assert rec["device"] == "rig" and rec["state"] == "checking"
        # The re-probe finds nothing listening there.
        world.down.add("rig")
        remote._DEVICES["rig"]["probe"] = "refused"
        rec = await fleet.probe_joiner("rig")
        assert rec["state"] == "unreachable_joiner"
        assert (
            "connection refused" in rec["reason"] and "Make reachable" in rec["reason"]
        )
        _, st = await world.ui("GET", "/api/fleet")
        assert st["admitted"][0]["state"] == "unreachable_joiner"
        # Fixed over there: the next probe says so.
        world.down.discard("rig")
        assert (await fleet.probe_joiner("rig"))["state"] == "reachable"


async def test_a_reachable_joiner_reads_as_reachable(world, monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    _, out = await _join_by_code(world, "rig", "laptop")
    assert out["self_reachable"] is True
    with world.on("laptop"):
        assert (await fleet.probe_joiner("rig"))["state"] == "reachable"
        assert await fleet.probe_joiner("nobody") == {}


# --------------------------------------------------------------------------- #
# rank 5: why a device isn't listed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "err, want",
    [
        (ConnectionRefusedError(), "refused"),
        (OSError(errno.ECONNREFUSED, "refused"), "refused"),
        (asyncio.TimeoutError(), "timeout"),
        (TimeoutError(), "timeout"),
        (OSError(errno.ETIMEDOUT, "timed out"), "timeout"),
        (ssl.SSLError("bad handshake"), "tls"),
        (ValueError("not json"), "not_mindflock"),
        (OSError(errno.EHOSTUNREACH, "no route"), "unreachable"),
        (RuntimeError("?"), "unreachable"),
    ],
)
def test_probe_outcome_classifies_errors(err, want):
    assert remote.probe_outcome(err) == want


@pytest.mark.skipif(aiohttp is None, reason="aiohttp not installed")
def test_probe_outcome_reads_aiohttp_connector_errors():
    from unittest import mock

    key = mock.Mock(host="100.64.0.2", port=8765, ssl=False)
    refused = aiohttp.ClientConnectorError(key, ConnectionRefusedError(111, "refused"))
    assert remote.probe_outcome(refused) == "refused"
    assert remote.plain_error(refused) == "connection refused"
    assert remote.probe_outcome(aiohttp.ServerTimeoutError()) == "timeout"


def test_better_keeps_the_primary_outcome_unless_a_later_one_says_more():
    assert remote._better("", "refused") == "refused"
    assert remote._better("timeout", "refused") == "timeout"
    assert remote._better("refused", "unreachable") == "refused"
    assert remote._better("refused", "http_404") == "http_404"
    assert remote._better("timeout", "tls") == "tls"


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    def __init__(self, answers):
        self.answers = answers

    def get(self, url, timeout=None):
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return _Resp(*a)


@pytest.mark.skipif(aiohttp is None, reason="aiohttp not installed")
@pytest.mark.parametrize(
    "answers, want",
    [
        ([ConnectionRefusedError(), asyncio.TimeoutError()], "refused"),
        ([asyncio.TimeoutError(), ConnectionRefusedError()], "timeout"),
        ([(404, None), ConnectionRefusedError()], "http_404"),
        ([(200, {"app": "other"}), asyncio.TimeoutError()], "not_mindflock"),
        ([ConnectionRefusedError(), ssl.SSLError("x")], "tls"),
    ],
)
def test_probe_peer_leaves_the_outcome_on_the_peer(monkeypatch, answers, want):
    async def session():
        return _Session(list(answers))

    monkeypatch.setattr(remote, "_http_session", session)
    peer = {"ip": "100.64.0.2", "dns": "rig.tail.ts.net"}
    assert asyncio.run(remote._probe_peer(peer)) is None
    assert peer["probe"] == want


@pytest.mark.parametrize(
    "outcome, needle",
    [
        ("refused", "connection refused on :8765"),
        ("timeout", "policy may block tcp:8765"),
        ("tls", "HTTPS"),
        ("http_502", "HTTP 502"),
        ("not_mindflock", "something other than MindFlock"),
        ("asleep", "offline in Tailscale"),
        ("unreachable", "unreachable"),
    ],
)
def test_probe_reason_wording(outcome, needle):
    assert needle in fleet.probe_reason(outcome, "rig", 8765)
    assert fleet.probe_reason("ok", "rig") == ""


def test_probe_reason_says_when_tailscale_last_saw_it():
    line = fleet.probe_reason(
        "asleep", "rig", 8765, ts_last_seen=1000.0, now=1000.0 + 7200
    )
    assert line == "asleep — Tailscale last saw it 2 h ago"


def test_discovery_records_why_each_peer_did_not_answer(monkeypatch):
    rig = {
        "key": "rig",
        "host": "Rig",
        "ip": "100.64.0.2",
        "dns": "rig.ts.net",
        "os": "linux",
        "online": True,
        "tags": ["tag:mindflock"],
        "ips": [],
    }
    box = {
        "key": "box",
        "host": "Box",
        "ip": "100.64.0.3",
        "dns": "box.ts.net",
        "os": "linux",
        "online": True,
        "tags": [],
        "ips": [],
    }
    monkeypatch.setattr(remote, "tailscale_nodes", lambda: (None, [rig, box]))
    monkeypatch.setattr(
        remote,
        "_OFFLINE",
        {
            "mini": {
                "key": "mini",
                "host": "Mini",
                "online": False,
                "tags": [],
                "ts_last_seen": 100.0,
            }
        },
    )

    async def probe(peer):
        peer["probe"] = "timeout" if peer["key"] == "rig" else "refused"
        return None

    monkeypatch.setattr(remote, "_probe_peer", probe)
    asyncio.run(remote._discover_once())
    got = {p["device"]: p for p in remote.probes()}
    assert got["rig"]["outcome"] == "timeout" and got["box"]["outcome"] == "refused"
    assert got["mini"]["outcome"] == "asleep" and got["mini"]["ts_last_seen"] == 100.0
    assert [p["device"] for p in remote.probes()][0] == "rig"  # tagged first
    assert remote._DEVICES["rig"]["probe"] == "timeout"
    peers = {p["device"]: p for p in fleet.status(True)["tailnet_peers"]}
    assert "policy may block" in peers["rig"]["reason"] and peers["rig"]["tagged"]
    assert "isn't running there" in peers["box"]["reason"]
    assert peers["mini"]["reason"].startswith("asleep — Tailscale last saw it")


def test_tailscale_nodes_keeps_offline_peers_aside(monkeypatch):
    doc = {
        "Self": {
            "DNSName": "me.ts.net.",
            "HostName": "me",
            "TailscaleIPs": ["100.1.1.1"],
        },
        "Peer": {
            "a": {
                "DNSName": "rig.ts.net.",
                "HostName": "rig",
                "Online": False,
                "TailscaleIPs": ["100.1.1.2"],
                "LastSeen": "2026-10-01T10:00:00Z",
                "OS": "linux",
            },
            "b": {
                "DNSName": "phone.ts.net.",
                "HostName": "phone",
                "Online": False,
                "TailscaleIPs": ["100.1.1.3"],
                "OS": "iOS",
            },
        },
    }
    monkeypatch.setattr(remote, "_tailscale_status", lambda: doc)
    _, peers = remote.tailscale_nodes()
    assert peers == []
    assert list(remote._OFFLINE) == ["rig"]  # phones never count
    assert remote._OFFLINE["rig"]["ts_last_seen"] > 0


async def test_an_offline_member_row_says_why(world):
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    world.down.add("rig")
    world.rediscover()
    remote._PROBES["rig"] = {
        "device": "rig",
        "host": "Rig",
        "outcome": "timeout",
        "tags": [],
        "ts_last_seen": 0.0,
        "online": True,
    }
    with world.on("laptop"):
        _, st = await world.ui("GET", "/api/fleet")
    rig = next(m for m in st["members"] if m["key"] == "rig")
    assert rig["reachable"] is False and "timed out on :8765" in rig["reason"]
    # A member is never ALSO listed as a stranger.
    assert all(p["device"] != "rig" for p in st["tailnet_peers"])
    assert '"grants"' in st["policy_grant"] and "tcp:8765" in st["policy_grant"]


# --------------------------------------------------------------------------- #
# §0.3: one "Paste a code" box
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, kind, code, device",
    [
        ("abcd-efgh", "device", "ABCD-EFGH", ""),
        ("ABCD EFGH", "device", "ABCD-EFGH", ""),
        ("mindflock devices join rig ABCD-EFGH", "device", "ABCD-EFGH", "rig"),
        ("rig abcdefgh", "device", "ABCD-EFGH", "rig"),
        ("mfp1:abcdefgh234567-wxyz", "peer", "mfp1:abcdefgh234567-wxyz", ""),
        ("Join me: MFP2:ABCDEFGH-WXYZ (10 min)", "peer", "mfp2:abcdefgh-wxyz", ""),
        ("hello there", "", "", ""),
        ("ABCD", "", "", ""),
    ],
)
def test_classify_code_routes_by_format(text, kind, code, device):
    got = fleet.classify_code(text)
    assert got == {"kind": kind, "code": code, "device": device}


# --------------------------------------------------------------------------- #
# rank 7: approve from wherever you are
# --------------------------------------------------------------------------- #
async def _mini_asks_laptop(world):
    """laptop + rig are your devices; mini asks laptop to let it in."""
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    with world.on("mini"):
        status, out = await world.ui("POST", "/api/fleet/request", {"device": "laptop"})
    assert status == 200 and out["state"] == "waiting", out
    return out


async def test_a_request_is_copied_to_every_member_and_approved_there(world, events):
    out = await _mini_asks_laptop(world)
    with world.on("rig"):
        _, st = await world.ui("GET", "/api/fleet")
        # (The harness's devices share one process, so laptop's own request
        # shows here too — the copy is the one with ``via``.)
        [req] = [r for r in st["requests"] if r["via"]]
        assert req["via"] == "laptop" and req["device"] == "mini"
        assert req["code"] == out["code"]  # the same code on every screen
    asked = [e["data"] for e in events if e["event"] == "device.join_requested"]
    assert [a.get("via", "") for a in asked] == ["", "laptop"]
    with world.on("rig"):
        status, res = await world.ui(
            "POST",
            "/api/fleet/requests/%s/approve" % req["id"],
            {"via": "laptop", "code": out["code"]},
        )
    assert status == 200, res
    assert res["device"] == "mini" and res["via"] == "laptop"
    with world.on("mini"):
        final = await _wait_join()
    assert final["state"] == "joined", final
    with world.on("laptop"):
        assert fleet.pending_requests() == []
    # The answered request left the copies too.
    with world.on("rig"):
        assert fleet.relayed_requests() == []
    member_approves = [c for c in world.calls if c[3] == "/api/fleet/member-approve"]
    assert member_approves == [
        ("rig", "laptop", "POST", "/api/fleet/member-approve", 200)
    ]


async def test_member_approve_with_a_wrong_fleet_key_is_403(world):
    out = await _mini_asks_laptop(world)
    with world.on("laptop"):
        [req] = fleet.pending_requests()
        r = await world.http.post(
            "/api/fleet/member-approve",
            json={"id": req["id"], "code": out["code"], "decision": "approve"},
            headers={
                "Authorization": "Bearer not-the-key-0123456789",
                remote.REMOTE_HEADER: "rig",
            },
        )
        assert r.status_code == 403
        # No credential at all, relayed: refused the same way.
        r = await world.http.post(
            "/api/fleet/member-approve",
            json={"id": req["id"], "code": out["code"]},
            headers={remote.REMOTE_HEADER: "rig"},
        )
        assert r.status_code == 403
        assert [x["id"] for x in fleet.pending_requests()] == [req["id"]]


async def test_a_relayed_approve_is_still_refused(world):
    await _mini_asks_laptop(world)
    with world.on("laptop"):
        [req] = fleet.pending_requests()
        r = await world.http.post(
            "/api/fleet/requests/%s/approve" % req["id"],
            json={},
            headers={remote.REMOTE_HEADER: "rig"},
        )
        assert r.status_code == 403
        # Not even naming a member to relay through.
        r = await world.http.post(
            "/api/fleet/requests/%s/approve" % req["id"],
            json={"via": "rig"},
            headers={remote.REMOTE_HEADER: "rig"},
        )
        assert r.status_code == 403
        assert len(fleet.pending_requests()) == 1


async def test_a_wrong_six_digit_code_is_rejected(world):
    out = await _mini_asks_laptop(world)
    wrong = "000 000" if out["code"] != "000 000" else "111 111"
    with world.on("laptop"):
        [req] = fleet.pending_requests()
        key = fleet.fleet_key()
        r = await world.http.post(
            "/api/fleet/member-approve",
            json={"id": req["id"], "code": wrong, "decision": "approve"},
            headers={"Authorization": "Bearer " + key},
        )
        assert r.status_code == 403 and "code" in r.json()["error"]
        assert len(fleet.pending_requests()) == 1  # still waiting
    # On the approving member, a code that isn't the one it showed.
    with world.on("rig"):
        status, res = await world.ui(
            "POST",
            "/api/fleet/requests/%s/approve" % req["id"],
            {"via": "laptop", "code": wrong},
        )
        assert status == 403
    # And a local approve given a code checks it too.
    with world.on("laptop"):
        status, _ = await world.ui(
            "POST", "/api/fleet/requests/%s/approve" % req["id"], {"code": wrong}
        )
        assert status == 403
        assert len(fleet.pending_requests()) == 1


async def test_deny_from_another_member(world):
    await _mini_asks_laptop(world)
    with world.on("rig"):
        [req] = fleet.relayed_requests()
        status, res = await world.ui(
            "POST", "/api/fleet/requests/%s/deny" % req["id"], {"via": "laptop"}
        )
    assert status == 200 and res["device"] == "mini"
    with world.on("mini"):
        final = await _wait_join()
    assert final["state"] == "denied"


async def test_pending_from_a_non_member_is_refused(world):
    await _join_by_code(world, "rig", "laptop")
    world.rediscover()
    with world.on("rig"):
        key = fleet.fleet_key()
        with pytest.raises(ValueError):
            fleet.hold_relayed({"from": "mini", "requests": []})
        with pytest.raises(ValueError):
            fleet.hold_relayed({"from": "rig", "requests": []})  # itself
        r = await world.http.post(
            "/api/fleet/pending",
            json={"from": "laptop", "requests": []},
            headers={"Authorization": "Bearer wrong-key-0123456789"},
        )
        assert r.status_code == 403
        r = await world.http.post(
            "/api/fleet/pending",
            json={
                "from": "laptop",
                "requests": [
                    {"id": "0123456789abcdef", "device": "mini", "code": "not a code"},
                    {
                        "id": "fedcba9876543210",  # pragma: allowlist secret
                        "device": "mini",
                        "code": "123 456",
                        "expires_at": 9e18,
                    },
                ],
            },
            headers={"Authorization": "Bearer " + key},
        )
        assert r.status_code == 200 and r.json()["held"] == 1
        [held] = fleet.relayed_requests()
        # A sender can't make its copy outlive a request's own lifetime.
        assert held["expires_at"] <= fleet._now() + fleet.REQUEST_TTL


def test_notify_skips_relayed_copies_and_deep_links_the_approve(monkeypatch):
    from backend.web.addons import notify
    from backend.web.core import mobile_announce, ntfy

    sent = []
    monkeypatch.setattr(ntfy, "load", lambda: type("C", (), {"active": True})())
    monkeypatch.setattr(ntfy, "publish_soon", lambda cfg, **kw: sent.append(kw))
    monkeypatch.setattr(mobile_announce, "click_for", lambda s="": "http://box:8765/m")
    monkeypatch.setattr(
        notify,
        "_enabled_rules",
        lambda: [r for r in notify.NOTIFY_RULES if r["id"] == "device_join"],
    )
    addon = notify.NotifyAddon()
    data = {
        "device": "mini",
        "host": "Mini",
        "code": "123 456",
        "id": "0123456789abcdef",
        "detail": "Mini · code 123 456",
    }
    addon._on_event(
        {"event": "device.join_requested", "session": "", "data": dict(data, via="rig")}
    )
    assert sent == []
    addon._on_event({"event": "device.join_requested", "session": "", "data": data})
    assert sent[0]["click"] == "http://box:8765/m#approve=0123456789abcdef"


# --------------------------------------------------------------------------- #
# rank 10: match my other devices + the phone-link table
# --------------------------------------------------------------------------- #
async def test_match_and_phone_link(world, monkeypatch):
    await _join_by_code(world, "rig", "laptop")
    await _join_by_code(world, "mini", "laptop")
    world.rediscover()
    remote._DEVICES["rig"].update(shared_link="mindflock", shared_link_live=True)
    remote._DEVICES["mini"].update(shared_link="mindflock", shared_link_live=False)
    monkeypatch.setenv("CS_WEB_MODE", "local")
    with world.on("laptop"):
        st = fleet.status(True)
    assert st["match"] == {"reachable": True, "shared_link": "mindflock"}
    link = st["phone_link"]
    assert link["name"] == "mindflock"
    states = {h["key"]: h["state"] for h in link["hosts"]}
    assert states == {"laptop": "off", "rig": "hosting", "mini": "waiting"}
    # Nothing to match once this one is reachable and names the same link.
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    with world.on("laptop"):
        from backend.config import settings as store

        store.update_settings(general={"shared_link": "mindflock"})
        st = fleet.status(True)
    assert st["match"] is None
    assert {h["key"]: h["state"] for h in st["phone_link"]["hosts"]}[
        "laptop"
    ] == "waiting"


def test_no_phone_link_and_no_match_outside_a_group():
    st = fleet.status(True)
    assert st["phone_link"] is None and st["match"] is None


def test_the_phone_page_has_an_approve_card():
    """/m shows join requests (its own and the copies it holds) with
    Approve / Deny; a push's tap lands on /m#approve=<id>."""
    from pathlib import Path

    static = Path(__file__).resolve().parents[2] / "backend" / "web" / "static"
    html = (static / "mobile.html").read_text()
    js = (static / "mobile.js").read_text()
    assert 'id="approve-cards"' in html
    assert 'fetch("/api/fleet")' in js
    assert '"/api/fleet/requests/" + encodeURIComponent(r.id) + "/" + decision' in js
    # The member it waits on and the code shown go along (relayed approve).
    assert 'JSON.stringify({ via: r.via || "", code: r.code || "" })' in js
    assert "approve=([0-9a-f]{16})" in js


def test_the_desktop_app_raises_os_notifications():
    """electron: a native Notification for what needs you while minimized
    (the page asks through window.mfnotify); its click focuses the window
    and names the screen; the shell's own update check notifies once per
    version."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "electron"
    main = (root / "main.js").read_text()
    pre = (root / "preload.js").read_text()
    assert "Notification } = require('electron')" in main
    assert "ipcMain.handle('notify:show'" in main
    assert "win.webContents.send('notify:click', target)" in main
    assert "NOTIFY_TARGETS = new Set(['devices', 'peer', 'update'])" in main
    # One notice per release: the shell's own update toast and the page's
    # update.available share one claim per version.
    assert "function claimUpdateNotice(version)" in main
    assert "if (!claimUpdateNotice(version)) return { ok: false }" in main
    assert "claimUpdateNotice(latest) && unfocused" in main
    assert "exposeInMainWorld('mfnotify'" in pre
    assert "ipcRenderer.invoke('notify:show'" in pre


def test_a_candidate_that_stopped_answering_says_why_on_its_own_row():
    remote._DEVICES["box"] = {
        "key": "box", "host": "Box", "reachable": False, "last_seen": 5.0,
        "fleet_proto": 1, "fleet": "", "version": "",
    }  # fmt: skip
    remote._PROBES["box"] = {
        "device": "box", "host": "Box", "outcome": "refused", "tags": [],
        "ts_last_seen": 0.0, "online": True,
    }  # fmt: skip
    st = fleet.status(True)
    [cand] = st["candidates"]
    assert "connection refused" in cand["reason"]
    assert st["tailnet_peers"] == []  # not listed twice


# --------------------------------------------------------------------------- #
# Make reachable's one save under the configure guard (POST /api/settings)
# --------------------------------------------------------------------------- #
from tests.unit.test_configure_guard import (  # noqa: E402,F401 — fixtures
    LOOPBACK,
    TAILNET,
    TOKEN,
    _client,
    app,
    untrusted,
)

REACHABLE = {"general": {"serve_mode": "tailscale", "auth_mode": "on"}}


def _general():
    from backend.config import settings as S

    S.invalidate()
    return S.load_settings().general


def test_make_reachable_saves_from_this_machine(app, monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "local")  # the device being fixed
    c = _client(app, LOOPBACK, base_url="http://127.0.0.1:8765")
    r = c.post("/api/settings", json=REACHABLE)
    assert r.status_code == 200, r.text
    g = _general()
    assert g.serve_mode == "tailscale" and g.auth_mode == "on"
    r = c.post("/api/settings", json={"general": {"shared_link": "mindflock"}})
    assert r.status_code == 200 and _general().shared_link == "mindflock"


def test_make_reachable_is_refused_remotely(app):
    from backend.config import settings as S

    r = _client(app, TAILNET).post("/api/settings", json=REACHABLE)
    assert r.status_code == 403
    S.update_settings(general={"remote_control": True})
    relayed = _client(
        app,
        LOOPBACK,
        headers={"Authorization": "Bearer " + TOKEN, "X-MindFlock-Remote": "otherbox"},
    )
    assert relayed.post("/api/settings", json=REACHABLE).status_code == 403
    assert _general().serve_mode != "tailscale"
