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

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import auth, mobile_access, mobile_announce, remote, shared_link

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
    """A fake ``tailscale`` CLI: records commands, answers ``status``."""
    calls: list = []
    state = {"rc": 0, "out": "", "status": {"MagicDNSSuffix": "tail1234.ts.net."}}

    def run(args):
        calls.append(args)
        if "--service=" in " ".join(args):
            return state["rc"], state["out"]
        return 0, ""

    monkeypatch.setattr(shared_link.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(shared_link, "_run", run)
    monkeypatch.setattr(shared_link, "_tailscale_status", lambda: state["status"])
    state["calls"] = calls
    return state


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
    st = shared_link.status()
    assert st["tagged"] is False and st["approved"] is False
    fake_ts["status"]["Self"] = {
        "Tags": ["tag:mindflock"],
        "CapMap": {"service-host": [{"svc:mindflock": ["100.100.1.1"]}]},
    }
    st = shared_link.status()
    assert st["tagged"] is True and st["approved"] is True


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
    serves = lambda: [c for c in fake_ts["calls"] if any("--service=" in a for a in c)]
    assert len(serves()) == 1
    client.post("/api/settings", json={"general": {"shared_link": "mindflock"}})
    assert len(serves()) == 1
