"""The shared phone link (:mod:`backend.web.core.shared_link`).

One Tailscale Service URL that every device advertises, so the phone keeps one
link whichever device is up. Covers the four places it lands:

* advertising / withdrawing (``tailscale serve`` is faked — no tailnet here),
* the auth gate: one QR carrying every device's token, a cookie per token, and
  the service hostname passing the local-mode rebinding guard,
* the URL every phone surface hands out (QR, banner, ntfy click target),
* the settings save that turns it on.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import auth, mobile_access, mobile_announce, remote, shared_link

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "tailscale_shared_link.json"
)

OWN = "own-token-aaaaaaaa"
PEER = "peer-token-bbbbbbbb"


@pytest.fixture(autouse=True)
def _isolate(isolate_settings_store, monkeypatch):
    monkeypatch.setattr(shared_link, "_STATE", {"name": "", "host": "", "error": ""})
    monkeypatch.setattr(auth, "_FRONTED_HOSTS", set())
    monkeypatch.setattr(remote, "_TOKENS", {})
    monkeypatch.setattr(remote, "_DEVICES", {})


@pytest.fixture
def fake_ts(monkeypatch):
    """A fake ``tailscale`` CLI: records commands, answers ``status``, and
    keeps a serve config that ``serve --service`` sets and ``serve clear``
    drops (what ``serve status --json`` reports)."""
    calls: list = []
    state = {
        "rc": 0,
        "out": "",
        "status": {"MagicDNSSuffix": "tail1234.ts.net."},
        "serve": {},
    }

    def run(args):
        calls.append(args)
        if "--service=" in " ".join(args):
            if state["rc"] == 0:
                svc = args[2].split("=", 1)[1]
                state["serve"].setdefault("Services", {})[svc] = _serve_entry(
                    svc, args[-1]
                )
            return state["rc"], state["out"]
        if args[:3] == ["tailscale", "serve", "clear"]:
            state["serve"].get("Services", {}).pop(args[3], None)
        return 0, ""

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(shared_link, "_run", run)
    monkeypatch.setattr(shared_link, "_tailscale_status", lambda: state["status"])
    monkeypatch.setattr(shared_link, "_serve_status", lambda: state["serve"])
    state["calls"] = calls
    return state


def _serve_entry(svc: str, target: str) -> dict:
    """One service in ``tailscale serve status --json``, shaped as captured."""
    host = "%s.tail1234.ts.net:443" % svc[4:]
    return {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {host: {"Handlers": {"/": {"Proxy": target}}}},
    }


def _serves(fake) -> list:
    return [c for c in fake["calls"] if any("--service=" in a for a in c)]


# --------------------------------------------------------------------------- #
# names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, want",
    [
        ("mindflock", "mindflock"),
        (" svc:MindFlock ", "mindflock"),
        ("my-fleet2", "my-fleet2"),
        ("", ""),
        ("-bad", ""),
        ("two.labels", ""),
        ("has space", ""),
    ],
)
def test_normalize(raw, want):
    assert shared_link.normalize(raw) == want


def test_setting_round_trip():
    S.update_settings(general={"shared_link": "mindflock"})
    assert S.load_settings().general.shared_link == "mindflock"
    assert shared_link.configured_name() == "mindflock"
    S.update_settings(general={"shared_link": ""})
    assert shared_link.configured_name() == ""


# --------------------------------------------------------------------------- #
# advertise / withdraw
# --------------------------------------------------------------------------- #
def test_apply_advertises_and_fronts_the_host(fake_ts):
    S.update_settings(general={"shared_link": "mindflock"})
    st = shared_link.apply(8765)
    serve = [c for c in fake_ts["calls"] if any("--service=" in a for a in c)]
    assert serve == [
        [
            "tailscale",
            "serve",
            "--service=svc:mindflock",
            "--https=443",
            "--bg",
            "--yes",
            "http://127.0.0.1:8765",
        ]
    ]
    # A stale/drained config is cleared first, so the serve is a fresh one.
    assert fake_ts["calls"][0] == ["tailscale", "serve", "clear", "svc:mindflock"]
    assert st["advertised"] is True and st["error"] == ""
    assert st["url"] == "https://mindflock.tail1234.ts.net/m"
    assert shared_link.advertised_url() == "https://mindflock.tail1234.ts.net/m"
    assert "mindflock.tail1234.ts.net" in auth._FRONTED_HOSTS


def test_turning_it_off_clears_the_service(fake_ts):
    S.update_settings(general={"shared_link": "mindflock"})
    shared_link.apply(8765)
    fake_ts["calls"].clear()
    S.update_settings(general={"shared_link": ""})
    st = shared_link.apply(8765)
    assert fake_ts["calls"] == [["tailscale", "serve", "clear", "svc:mindflock"]]
    assert st == {"enabled": False}
    assert shared_link.advertised_url() is None
    assert not auth._FRONTED_HOSTS


def test_renaming_withdraws_the_old_name(fake_ts):
    S.update_settings(general={"shared_link": "old"})
    shared_link.apply(8765)
    S.update_settings(general={"shared_link": "new"})
    shared_link.apply(8765)
    flat = [" ".join(c) for c in fake_ts["calls"]]
    assert "tailscale serve clear svc:old" in flat
    assert shared_link.advertised_url() == "https://new.tail1234.ts.net/m"
    assert auth._FRONTED_HOSTS == {"new.tail1234.ts.net"}


def test_refusal_is_explained(fake_ts):
    fake_ts["rc"], fake_ts["out"] = 1, "Access denied: serve config denied"
    S.update_settings(general={"shared_link": "mindflock"})
    st = shared_link.apply(8765)
    assert st["advertised"] is False
    assert "--operator" in st["error"]
    assert shared_link.advertised_url() is None


def test_withdraw_only_touches_what_this_process_advertised(fake_ts):
    shared_link.withdraw()
    assert fake_ts["calls"] == []
    S.update_settings(general={"shared_link": "mindflock"})
    shared_link.apply(8765)
    fake_ts["calls"].clear()
    shared_link.withdraw()
    assert fake_ts["calls"] == [["tailscale", "serve", "clear", "svc:mindflock"]]
    assert shared_link.advertised_url() is None


def test_status_reads_tag_and_approval(fake_ts):
    S.update_settings(general={"shared_link": "mindflock"})
    shared_link.apply(8765)
    fake_ts["status"]["Self"] = {"Tags": [], "CapMap": {}}
    st = shared_link.status()
    assert st["tagged"] is False and st["approved"] is False
    fake_ts["status"]["Self"] = {
        "Tags": ["tag:mindflock"],
        "CapMap": {"service-host": [{"svc:mindflock": ["100.100.1.1"]}]},
        "AllowedIPs": ["100.64.0.1/32", "100.100.1.1/32"],
    }
    st = shared_link.status()
    assert st["tagged"] is True and st["approved"] is True and st["routed"] is True


# --------------------------------------------------------------------------- #
# reading approval from captured `tailscale status --json` (sanitized)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def captured():
    return json.loads(FIXTURE.read_text())


def test_captured_approved_host(captured):
    data = captured["status_approved"]
    assert shared_link._tagged(data) is True
    assert shared_link._defined(data, "mindflock") is True
    assert shared_link._approved(data, "mindflock") is True
    assert shared_link._routed(data, "mindflock") is True
    # Another service on the same tailnet: this device hosts nothing of it.
    assert shared_link._approved(data, "other") is False


def test_captured_pending_host_is_not_approved(captured):
    """The bug: ``services/<name>`` is in CapMap while the advertisement is
    pending, and was read as approval — the UI showed ✓ while the phone timed
    out. Only ``service-host`` (plus its routed VIPs) is approval."""
    data = captured["status_pending"]
    assert "services/mindflock" in data["Self"]["CapMap"]
    assert shared_link._defined(data, "mindflock") is True
    assert shared_link._approved(data, "mindflock") is False
    assert shared_link._routed(data, "mindflock") is None


def test_captured_untagged_host(captured):
    data = captured["status_untagged"]
    assert shared_link._tagged(data) is False
    assert shared_link._approved(data, "mindflock") is False
    # Not advertised and no DNS record: unseen, which is not "undefined".
    assert shared_link._defined(data, "mindflock") is None


def test_host_vips_not_routed_yet(captured):
    data = copy.deepcopy(captured["status_approved"])
    data["Self"]["AllowedIPs"] = data["Self"]["TailscaleIPs"]
    assert shared_link._approved(data, "mindflock") is True
    assert shared_link._routed(data, "mindflock") is False


@pytest.mark.parametrize("status", [{}, {"Self": {"Tags": ["tag:x"]}}])
def test_unreadable_approval_is_unknown_not_a_tick(status):
    assert shared_link._approved(status, "mindflock") is None
    assert shared_link._routed(status, "mindflock") is None


def test_captured_machine_names_the_deduplicated_name(captured):
    m = shared_link._machine(captured["status_approved"])
    assert m == {
        "hostname": "Box",
        "dns": "box-1.tail0000.ts.net",
        "ip": "100.64.0.10",
        "duplicate_of": "box",
    }
    peer = copy.deepcopy(captured["status_approved"])
    peer["Peer"] = {}
    assert shared_link._machine(peer)["duplicate_of"] == ""


def test_captured_serve_status(captured):
    serve = captured["serve"]
    assert shared_link._serve_live(serve, "mindflock", 8765) is True
    assert shared_link._serve_live(serve, "mindflock", 9000) is False
    assert shared_link._serve_live({}, "mindflock", 8765) is False
    assert shared_link._serve_live(None, "mindflock", 8765) is None


def _step(st, sid):
    return next(s for s in st["steps"] if s["id"] == sid)


def test_checklist_from_captured_states(fake_ts, captured):
    S.update_settings(general={"shared_link": "mindflock"})
    fake_ts["status"] = copy.deepcopy(captured["status_pending"])
    st = shared_link.apply(8765)
    assert [s["id"] for s in st["steps"]] == [
        "operator",
        "tag",
        "define",
        "policy",
        "approval",
        "phone",
    ]
    assert _step(st, "operator")["state"] == "ok"
    assert _step(st, "tag")["state"] == "ok"
    assert _step(st, "define")["state"] == "ok"
    assert _step(st, "policy")["state"] == "unknown"
    assert _step(st, "approval")["state"] == "fail"
    assert "hasn't made this device a host" in _step(st, "approval")["reason"]
    assert _step(st, "phone")["state"] == "unknown"
    # Prefilled with this device's own tag and the service name.
    assert '"tag:mindflock": ["autogroup:admin"]' in st["policy"]
    assert '"svc:mindflock": ["tag:mindflock"]' in st["policy"]
    assert '"dst": ["svc:mindflock"], "ip": ["tcp:443"]' in st["policy"]
    assert "grants" not in st  # one block now, not two colliding snippets
    assert st["machine"]["duplicate_of"] == "box"

    fake_ts["status"] = copy.deepcopy(captured["status_approved"])
    st = shared_link.status()
    assert {s["id"]: s["state"] for s in st["steps"]} == {
        "operator": "ok",
        "tag": "ok",
        "define": "ok",
        "policy": "ok",
        "approval": "ok",
        "phone": "unknown",
    }


def test_checklist_untagged_and_unknown(fake_ts, captured):
    S.update_settings(general={"shared_link": "mindflock"})
    fake_ts["status"] = copy.deepcopy(captured["status_untagged"])
    st = shared_link.apply(8765)
    assert _step(st, "tag")["state"] == "fail"
    assert "box-1.tail0000.ts.net has no tag" in _step(st, "tag")["reason"]
    assert _step(st, "define")["state"] == "unknown"
    assert "untagged" in _step(st, "approval")["reason"]
    assert st["tag"] == "tag:mindflock"  # the suggested tag
    fake_ts["status"] = {}
    st = shared_link.status()
    assert _step(st, "tag")["state"] == "unknown"
    assert _step(st, "approval")["state"] == "unknown"


def test_checklist_operator_refusal(fake_ts):
    fake_ts["rc"], fake_ts["out"] = 1, "Access denied: serve config denied"
    S.update_settings(general={"shared_link": "mindflock"})
    st = shared_link.apply(8765)
    assert st["error_kind"] == "operator"
    assert _step(st, "operator")["state"] == "fail"
    assert st["operator_fix"] == "sudo tailscale set --operator=$USER"


# --------------------------------------------------------------------------- #
# idempotent apply: reconcile only re-serves on drift
# --------------------------------------------------------------------------- #
def test_reconcile_is_a_no_op_while_everything_matches(fake_ts):
    S.update_settings(general={"shared_link": "mindflock"})
    shared_link.apply(8765)
    fake_ts["calls"].clear()
    st = shared_link.reconcile(8765)
    assert fake_ts["calls"] == []
    assert st["advertised"] is True


def test_reconcile_reserves_when_serve_config_was_cleared(fake_ts):
    S.update_settings(general={"shared_link": "mindflock"})
    shared_link.apply(8765)
    fake_ts["serve"].clear()  # `tailscale serve reset` behind our back
    assert shared_link.status()["advertised"] is False
    fake_ts["calls"].clear()
    st = shared_link.reconcile(8765)
    assert len(_serves(fake_ts)) == 1
    assert st["advertised"] is True


def test_reconcile_reserves_when_the_port_changed(fake_ts):
    S.update_settings(general={"shared_link": "mindflock"})
    shared_link.apply(8765)
    fake_ts["calls"].clear()
    shared_link.reconcile(9000)
    assert _serves(fake_ts)[0][-1] == "http://127.0.0.1:9000"


def test_reconcile_readvertises_once_the_device_is_tagged(fake_ts, captured):
    """Advertised before tagging, the auto-approver never looked again: the
    tag appearing is what re-advertises (once — not every tick)."""
    S.update_settings(general={"shared_link": "mindflock"})
    fake_ts["status"] = copy.deepcopy(captured["status_untagged"])
    shared_link.apply(8765)
    fake_ts["calls"].clear()
    shared_link.reconcile(8765)
    assert _serves(fake_ts) == []
    fake_ts["status"] = copy.deepcopy(captured["status_pending"])  # now tagged
    shared_link.reconcile(8765)
    assert len(_serves(fake_ts)) == 1
    shared_link.reconcile(8765)
    assert len(_serves(fake_ts)) == 1


def test_recheck_nudges_a_tagged_unapproved_host(fake_ts, captured):
    S.update_settings(general={"shared_link": "mindflock"})
    fake_ts["status"] = copy.deepcopy(captured["status_pending"])
    shared_link.apply(8765)
    fake_ts["calls"].clear()
    shared_link.reconcile(8765, nudge=True)
    assert len(_serves(fake_ts)) == 1
    # An approved host is left alone even on a nudge.
    fake_ts["status"] = copy.deepcopy(captured["status_approved"])
    shared_link.reconcile(8765)  # tags unchanged, serve live
    fake_ts["calls"].clear()
    shared_link.reconcile(8765, nudge=True)
    assert fake_ts["calls"] == []


def test_reconcile_retries_after_a_failure(fake_ts):
    fake_ts["rc"], fake_ts["out"] = 1, "Access denied"
    S.update_settings(general={"shared_link": "mindflock"})
    shared_link.apply(8765)
    fake_ts["rc"], fake_ts["out"] = 0, ""  # the user ran --operator
    st = shared_link.reconcile(8765)
    assert st["advertised"] is True and st["error"] == ""


def _run_loop_briefly(want_calls: list, n: int) -> None:
    async def go():
        task = asyncio.create_task(shared_link.recheck_loop(lambda: 8765))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(want_calls) >= n:
                break
        task.cancel()

    asyncio.run(go())


def test_recheck_loop_reconciles_only_while_on(monkeypatch):
    calls: list = []
    monkeypatch.setattr(shared_link, "RECHECK_INTERVAL", 0)
    monkeypatch.setattr(shared_link, "reconcile", lambda port: calls.append(port))
    _run_loop_briefly(calls, 1)
    assert calls == []  # off: no shell-outs at all
    S.update_settings(general={"shared_link": "mindflock"})
    _run_loop_briefly(calls, 2)
    assert calls[:2] == [8765, 8765]


def test_reconcile_never_raises_without_tailscale(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    S.update_settings(general={"shared_link": "mindflock"})
    st = shared_link.reconcile(8765)
    assert st["advertised"] is False
    assert st["error_kind"] == "missing"
    assert st["approved"] is None and st["tagged"] is None


# --------------------------------------------------------------------------- #
# auth: one QR, every device's token
# --------------------------------------------------------------------------- #
@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", OWN)
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    return TestClient(server.app)


def test_multi_token_qr_stores_a_cookie_per_token(gate):
    r = gate.get(
        "/m?token=%s&token=%s" % (PEER, OWN),
        follow_redirects=False,
        headers={"accept": "text/html"},
    )
    assert r.status_code == 302 and "token" not in r.headers["location"]
    set_cookie = r.headers.get_list("set-cookie")
    names = {c.split("=", 1)[0] for c in set_cookie}
    assert auth.COOKIE_NAME in names
    assert auth.cookie_name_for(OWN) in names
    # The peer's token rides along so the PEER finds it when it answers.
    assert auth.cookie_name_for(PEER) in names


def test_a_qr_with_no_token_of_ours_is_refused(gate):
    r = gate.get("/api/instances?token=%s" % PEER)
    assert r.status_code == 401


def test_keyed_cookie_signs_in_when_the_plain_one_is_another_devices(gate):
    # The plain cookie was last set by another device on the shared origin.
    gate.cookies.set(auth.COOKIE_NAME, PEER)
    assert gate.get("/api/instances").status_code == 401
    gate.cookies.set(auth.cookie_name_for(OWN), OWN)
    assert gate.get("/api/instances").status_code == 200


def test_a_foreign_keyed_cookie_alone_is_not_enough(gate):
    gate.cookies.set(auth.cookie_name_for(PEER), PEER)
    assert gate.get("/api/instances").status_code == 401


def test_login_sets_the_keyed_cookie_too(gate):
    r = gate.post("/api/auth", json={"token": OWN})
    names = {c.split("=", 1)[0] for c in r.headers.get_list("set-cookie")}
    assert {auth.COOKIE_NAME, auth.cookie_name_for(OWN)} <= names


def test_unsafe_tokens_are_never_echoed_into_cookies():
    from starlette.responses import Response

    resp = auth.set_auth_cookies(Response(), extra=["bad;token=x", "ok-token-123456"])
    raw = " ".join(resp.headers.getlist("set-cookie"))
    assert "bad;" not in raw and auth.cookie_name_for("ok-token-123456") in raw


def test_local_mode_accepts_the_fronted_service_host(monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "local")
    scope = {"headers": [(b"host", b"mindflock.tail1234.ts.net")]}
    assert auth.host_ok(scope) is False
    auth.allow_fronted_host("mindflock.tail1234.ts.net")
    assert auth.host_ok(scope) is True
    assert auth.host_ok({"headers": [(b"host", b"evil.example")]}) is False


# --------------------------------------------------------------------------- #
# what the phone surfaces hand out
# --------------------------------------------------------------------------- #
@pytest.fixture
def advertised(fake_ts):
    S.update_settings(general={"shared_link": "mindflock"})
    shared_link.apply(8765)
    return "https://mindflock.tail1234.ts.net/m"


def test_mobile_info_leads_with_the_shared_link(advertised, monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", OWN)
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    monkeypatch.setattr(
        server, "_tailscale_info", lambda: ("box.tail1234.ts.net", "100.1.2.3")
    )
    monkeypatch.setattr(server, "_tailscale_serves_port", lambda port: False)
    remote._TOKENS["otherbox"] = PEER
    info = server._mobile_info()
    assert info["urls"][1] == {"label": "Shared link", "url": advertised}
    assert {"label": "This device", "url": "http://box.tail1234.ts.net:8765/m"} in (
        info["urls"]
    )
    assert info["qr_target"] == "%s?token=%s&token=%s" % (advertised, OWN, PEER)
    assert info["shared"]["advertised"] is True


def test_shared_link_works_in_local_mode(advertised, monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "local")
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    info = server._mobile_info()
    assert info["qr_target"] == advertised
    assert info["note"] is None
    assert mobile_access.tailnet_url() == (advertised, True)


def test_banner_prints_the_shared_link(advertised, monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "local")
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setattr(server, "_qr_lines", lambda data: "QR(%s)" % data)
    banner = server._mobile_banner()
    assert "Shared:     %s" % advertised in banner
    assert "QR(%s)" % advertised in banner


def test_push_deep_link_names_the_device(advertised, monkeypatch):
    monkeypatch.setattr(remote, "_SELF", {"key": "box", "host": "box"})
    mobile_announce.remember_url(advertised)
    assert mobile_announce.click_for("alpha") == advertised + "?s=box%3A%3Aalpha"


def test_hello_reports_the_link():
    S.update_settings(general={"shared_link": "mindflock"})
    assert remote.hello_json()["shared_link"] == "mindflock"


# --------------------------------------------------------------------------- #
# the settings save
# --------------------------------------------------------------------------- #
def test_settings_save_applies_and_validates(monkeypatch):
    applied = []
    monkeypatch.setattr(
        shared_link, "apply", lambda port: applied.append(port) or {"advertised": False}
    )
    client = TestClient(server.app)
    r = client.post("/api/settings", json={"general": {"shared_link": "no spaces"}})
    assert r.status_code == 400
    assert applied == []
    r = client.post("/api/settings", json={"general": {"shared_link": "svc:MindFlock"}})
    assert r.status_code == 200
    assert S.load_settings().general.shared_link == "mindflock"
    assert len(applied) == 1
    # The same name again while it isn't up is the retry after a fix.
    client.post("/api/settings", json={"general": {"shared_link": "mindflock"}})
    assert len(applied) == 2


def test_resaving_a_live_link_does_not_reserve(fake_ts):
    client = TestClient(server.app)
    client.post("/api/settings", json={"general": {"shared_link": "mindflock"}})
    assert len(_serves(fake_ts)) == 1
    fake_ts["status"]["Self"] = {
        "Tags": ["tag:mindflock"],
        "CapMap": {"service-host": [{"svc:mindflock": ["100.100.1.1"]}]},
        "AllowedIPs": ["100.100.1.1/32"],
    }
    client.post("/api/settings", json={"general": {"shared_link": "mindflock"}})
    # Tags changed since the apply (none → tag:mindflock): one re-serve ...
    assert len(_serves(fake_ts)) == 2
    client.post("/api/settings", json={"general": {"shared_link": "mindflock"}})
    # ... then nothing drifted and it's approved: no re-serve.
    assert len(_serves(fake_ts)) == 2


def test_resaving_reapplies_when_serve_config_was_cleared(fake_ts):
    """Re-saving the same value used to trust this process's memory: with the
    serve config cleared underneath, nothing re-applied."""
    client = TestClient(server.app)
    client.post("/api/settings", json={"general": {"shared_link": "mindflock"}})
    fake_ts["serve"].clear()
    r = client.post("/api/settings", json={"general": {"shared_link": "mindflock"}})
    assert len(_serves(fake_ts)) == 2
    assert r.json()["shared_link"]["advertised"] is True


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@pytest.fixture
def no_device_probes(monkeypatch):
    """Keep ``_mobile_info`` off this machine's real tailscale."""
    monkeypatch.setattr(server, "_tailscale_info", lambda: (None, None))
    monkeypatch.setattr(server, "_tailscale_serves_port", lambda port: False)


def test_recheck_route_returns_the_mobile_payload(
    fake_ts, captured, monkeypatch, no_device_probes
):
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setattr(server, "_server_port", lambda: 8765)
    S.update_settings(general={"shared_link": "mindflock"})
    fake_ts["status"] = copy.deepcopy(captured["status_pending"])
    shared_link.apply(8765)
    fake_ts["calls"].clear()
    r = TestClient(server.app).post("/api/mobile/shared/recheck")
    assert r.status_code == 200
    body = r.json()
    assert len(_serves(fake_ts)) == 1  # nudged: tagged but unapproved
    shared = body["shared"]
    assert {"urls", "qr_svg", "shared"} <= set(body)
    assert [s["id"] for s in shared["steps"]][:2] == ["operator", "tag"]
    assert shared["approved"] is False and shared["defined"] is True
    assert shared["machine"]["dns"] == "box-1.tail0000.ts.net"


def test_mobile_route_payload_carries_the_checklist(
    fake_ts, captured, monkeypatch, no_device_probes
):
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    S.update_settings(general={"shared_link": "mindflock"})
    fake_ts["status"] = copy.deepcopy(captured["status_approved"])
    shared_link.apply(8765)
    shared = TestClient(server.app).get("/api/mobile").json()["shared"]
    for key in (
        "steps",
        "machine",
        "tag",
        "policy",
        "operator_fix",
        "admin",
        "approved",
        "routed",
        "defined",
    ):
        assert key in shared, key
    assert all(s["state"] in ("ok", "fail", "unknown") for s in shared["steps"])


# --------------------------------------------------------------------------- #
# the policy block
# --------------------------------------------------------------------------- #
def _hujson(text: str):
    """The block as the policy editor would parse it (HuJSON: comments and
    trailing commas allowed), wrapped in braces."""
    import re

    body = re.sub(r"//[^\n]*", "", text)
    body = re.sub(r",(\s*[}\]])", r"\1", "{" + body + "}")
    body = re.sub(r",\s*}$", "}", body)
    return json.loads(body)


def test_policy_block_is_one_complete_parseable_block():
    doc = _hujson(shared_link.policy_block("mindflock", "tag:mindflock", 9000))
    assert set(doc) == {"tagOwners", "autoApprovers", "grants", "tests"}
    assert doc["tagOwners"] == {"tag:mindflock": ["autogroup:admin"]}
    assert doc["autoApprovers"] == {"services": {"svc:mindflock": ["tag:mindflock"]}}
    grants = {(g["src"][0], g["dst"][0]): g["ip"] for g in doc["grants"]}
    # Device to device on the REAL server port, and 443 for the shared link.
    assert grants[("tag:mindflock", "tag:mindflock")] == ["tcp:9000", "tcp:443"]
    assert grants[("autogroup:member", "tag:mindflock")] == ["tcp:9000", "tcp:443"]
    assert grants[("autogroup:member", "svc:mindflock")] == ["tcp:443"]
    assert doc["tests"] == [{"src": "tag:mindflock", "accept": ["tag:mindflock:9000"]}]


def test_policy_block_lines_move_into_an_existing_key():
    # Merge-safe: every entry line inside a key ends in a comma, so moving it
    # into a tagOwners the policy already has needs no editing.
    text = shared_link.policy_block("mindflock", "tag:mindflock")
    assert '  "tag:mindflock": ["autogroup:admin"],\n' in text
    assert "can't appear twice" in text


def test_status_policy_uses_the_server_port(fake_ts, captured, monkeypatch):
    S.update_settings(general={"shared_link": "mindflock"})
    monkeypatch.setattr(mobile_access, "_server_port", lambda: 9123)
    fake_ts["status"] = copy.deepcopy(captured["status_pending"])
    st = shared_link.apply(9123)
    assert '"tcp:9123"' in st["policy"]
