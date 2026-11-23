"""End-to-end: Shared files across one person's devices (core.hub_remote).

Real servers (the fleet e2e harness, tests/integration/test_fleet_e2e.py):
alpha (gate OFF) and beta (gate ON) become one person's devices; rogue
(gate OFF) shares the "tailnet" but never joins. Every request alpha makes
to beta rides the real forwarding path ``/api/devices/beta/fwd/…``.

Proves: alpha lists beta's items; a fetch lands in alpha's own store with its
origin, byte-for-byte, and a second use doesn't fetch again; a code typed on
alpha for beta's item is fetched then expanded; a secret-looking item needs
the person's confirmation; a thumbnail is served checked; an item kept on
beta only and beta's privacy switch are honoured by beta; the stranger, an
unauthenticated caller and a relayed request get nothing; an offline device
is named as offline.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.integration.test_fleet_e2e import (  # noqa: F401
    NODES,
    Server,
    _free_port_on_all,
    _members,
    httpx,
    pytestmark,
    wait_for,
)

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
    b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def pair(tmp_path_factory):
    import socket
    import stat

    for name in ("alpha", "beta", "rogue"):
        s = socket.socket()
        try:
            s.bind((NODES[name]["ip"], 0))
        except OSError:
            pytest.skip("cannot bind %s" % NODES[name]["ip"])
        finally:
            s.close()
    root = tmp_path_factory.mktemp("hubfleet")
    stub_bin = root / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "tailscale"
    stub.write_text("#!/bin/sh\necho 'tailscale disabled in tests' >&2\nexit 1\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    port = _free_port_on_all()
    servers = {n: Server(n, port, root, stub_bin) for n in ("alpha", "beta", "rogue")}
    try:
        for srv in servers.values():
            wait_for(lambda s=srv: s.get("/api/remote/hello"), timeout=60)
        for srv in servers.values():
            srv.refresh()
        yield servers
    finally:
        for srv in servers.values():
            try:
                srv.stop()
            except Exception:  # noqa: BLE001 — already stopped by the test
                pass
        if os.environ.get("MINDFLOCK_E2E_LOGS") == "1":
            for srv in servers.values():
                print("==== %s ====" % srv.name)
                print((srv.dir / "server.log").read_text(errors="replace")[-6000:])


def _join(alpha: Server, beta: Server) -> None:
    invite = alpha.post("/api/fleet/invite")
    beta.post("/api/fleet/join", {"device": "alpha", "code": invite["code"]})
    wait_for(lambda: beta.fleet()["in_fleet"])
    for srv in (alpha, beta):
        srv.refresh()
    for srv in (alpha, beta):
        wait_for(lambda s=srv: _members(s) == {"alpha", "beta"}, timeout=45, every=1.0)
    for srv in (alpha, beta):
        srv.post("/api/settings", {"general": {"remote_control": "on"}})
        srv.refresh()


def _devices(srv: Server, fresh: bool = True) -> dict:
    body = srv.get("/api/hub/devices", params={"fresh": 1 if fresh else 0})
    return {d["device"]: d for d in body["devices"]}


def _beta_ok(alpha: Server) -> dict:
    def ok():
        alpha.refresh()
        d = _devices(alpha).get("beta")
        return d if d and d["state"] == "ok" else None

    return wait_for(ok, timeout=45, every=1.0)


def test_shared_files_between_two_of_my_devices(pair):
    alpha, beta, rogue = pair["alpha"], pair["beta"], pair["rogue"]
    _join(alpha, beta)

    # --- beta's items -------------------------------------------------------
    r = beta.http.post("/api/hub/items?name=shot.png", content=PNG)
    assert r.status_code == 201, r.text
    png = r.json()
    r = beta.http.post("/api/hub/text", json={"text": "hello from beta\n"})
    assert r.status_code == 201, r.text
    txt = r.json()
    r = beta.http.post("/api/hub/text", json={"text": "second note\n"})
    third = r.json()
    r = beta.http.post("/api/hub/items?name=.env&secret_ok=1", content=b"TOKEN=abc\n")
    assert r.status_code == 201, r.text
    env = r.json()

    # --- alpha lists them, marked with beta ----------------------------------
    d = _beta_ok(alpha)
    by_id = {it["id"]: it for it in d["items"]}
    assert set(by_id) >= {png["id"], txt["id"], env["id"], third["id"]}
    assert by_id[png["id"]]["sha256"] == png["sha256"]
    assert by_id[png["id"]]["device_name"] and by_id[png["id"]]["copy"] is None
    assert by_id[env["id"]]["secret"] is True and by_id[env["id"]]["preview"] == ""
    assert "path" not in by_id[png["id"]]  # never a path on beta's disk
    assert alpha.get("/api/hub")["items"] == []  # nothing was copied to list

    # --- a fetch lands in alpha's own store, byte-for-byte -------------------
    r = alpha.http.post(
        "/api/hub/devices/fetch", json={"id": "file@beta:%s" % png["id"]}
    )
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["already"] is False and got["origin"]["device"] == "beta"
    assert got["origin"]["id"] == png["id"] and got["sha256"] == png["sha256"]
    assert got["id"] == png["id"]  # the code was free here: same code both sides
    assert got["kind"] == "image" and got["by"] == "device"
    assert open(got["path"], "rb").read() == PNG
    assert got["path"].startswith(str(alpha.home))
    # A second use finds it: no second copy.
    r = alpha.http.post(
        "/api/hub/devices/fetch", json={"id": "file@beta:%s" % png["id"]}
    )
    assert r.json()["already"] is True and r.json()["id"] == got["id"]
    assert len(alpha.get("/api/hub")["items"]) == 1
    assert _devices(alpha)["beta"]["items"][0]["copy"] in (None, got["id"])
    # beta's own store is untouched by any of it.
    assert {i["id"] for i in beta.get("/api/hub")["items"]} == {
        png["id"],
        txt["id"],
        env["id"],
        third["id"],
    }

    # --- a code typed on alpha for beta's item: fetched, then expanded -------
    r = alpha.http.post("/api/hub/expand", json={"text": "see file:%s" % txt["id"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["unknown"] == [], out
    assert "MindFlock shared file" in out["text"] and str(alpha.home) in out["text"]
    copy = next(
        i
        for i in alpha.get("/api/hub")["items"]
        if (i.get("origin") or {}).get("id") == txt["id"]
    )
    assert copy["kind"] == "text" and copy["origin"]["device"] == "beta"
    # The device form works too.
    r = alpha.http.post(
        "/api/hub/expand", json={"text": "and file@beta:%s" % third["id"]}
    )
    assert r.json()["unknown"] == [] and str(alpha.home) in r.json()["text"]
    # A code no device holds stays as typed, said plainly.
    r = alpha.http.post("/api/hub/expand", json={"text": "file:zzzz"})
    assert r.json()["unknown"] == ["file:zzzz"] and r.json()["note"]

    # --- a secret-looking item needs the person's confirmation ---------------
    r = alpha.http.post("/api/hub/devices/fetch", json={"id": "file@beta:" + env["id"]})
    assert r.status_code == 403 and r.json()["code"] == "secret", r.text
    assert r.json()["can_confirm"] is True
    r = alpha.http.post(
        "/api/hub/devices/fetch",
        json={"id": "file@beta:" + env["id"], "secret_ok": True},
    )
    assert r.status_code == 200, r.text

    # --- a thumbnail, checked here -------------------------------------------
    r = alpha.http.get("/api/hub/devices/beta/items/%s/thumb" % png["id"])
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    r = alpha.http.get("/api/hub/devices/beta/items/%s/thumb" % txt["id"])
    assert r.status_code == 404

    # --- mixed selection: fetch + read-only refusals, per item ---------------
    r = alpha.http.post(
        "/api/hub/batch",
        json={"action": "pin", "ids": [got["ref"], "file@beta:" + third["id"]]},
    )
    res = r.json()["results"]
    assert res[0]["ok"] is True and res[1]["ok"] is False
    assert res[1]["code"] == "read_only"

    # --- beta keeps one item to itself; then turns offering off --------------
    r = beta.http.patch("/api/hub/items/" + third["id"], json={"local_only": True})
    assert r.status_code == 200 and r.json()["local_only"] is True
    ids = {it["id"] for it in _devices(alpha)["beta"]["items"]}
    assert third["id"] not in ids and png["id"] in ids
    beta.post("/api/settings", {"hub": {"offer_to_devices": False}})
    d = _devices(alpha)["beta"]
    assert d["state"] == "private" and d["items"] == [] and d["note"]
    beta.post("/api/settings", {"hub": {"offer_to_devices": True}})

    # --- nobody else gets beta's list ----------------------------------------
    base = "http://%s:%d" % (beta.ip, beta.port)
    # the stranger on the tailnet: the marker, no credential
    r = httpx.get(base + "/api/hub/offer", headers={"X-MindFlock-Remote": "rogue"})
    assert r.status_code in (401, 403), r.text
    assert png["sha256"] not in r.text
    # a wrong credential
    r = httpx.get(
        base + "/api/hub/offer",
        headers={"X-MindFlock-Remote": "rogue", "Authorization": "Bearer nope-0000"},
    )
    assert r.status_code in (401, 403)
    # beta's own token without the device marker: not a device request
    r = beta.http.get("/api/hub/offer")
    assert r.status_code == 403 and r.json()["code"] == "not_a_device"
    # a relayed request: alpha never forwards a request that is already remote
    r = alpha.http.get(
        "/api/devices/beta/fwd/api/hub/offer", headers={"X-MindFlock-Remote": "rogue"}
    )
    assert r.status_code == 400 and png["sha256"] not in r.text
    # nothing but the three offered GETs is forwarded
    for method, path in (
        ("DELETE", "/api/hub/items/" + png["id"]),
        ("GET", "/api/hub"),
        ("GET", "/api/hub/items/%s/raw" % png["id"]),
        ("POST", "/api/hub/offer"),
        ("GET", "/api/hub/offer/../hub"),
    ):
        r = alpha.http.request(method, "/api/devices/beta/fwd" + path)
        assert r.status_code in (400, 404, 405), (method, path, r.text)
    assert len(beta.get("/api/hub")["items"]) == 4
    # rogue is not one of the person's devices: it lists nothing of beta's
    rd = _devices(rogue)
    assert "beta" not in rd or rd["beta"]["items"] == []

    # --- beta goes away: named as offline, alpha's copies stay ---------------
    beta.stop()
    for srv in (alpha,):
        srv.refresh()
    wait_for(
        lambda: _devices(alpha).get("beta", {}).get("state") == "offline",
        timeout=60,
        every=2.0,
    )
    assert any(i["id"] == got["id"] for i in alpha.get("/api/hub")["items"])
    r = alpha.http.post("/api/hub/expand", json={"text": "file@beta:abcd"})
    assert r.json()["unknown"] == ["file@beta:abcd"]
    assert "beta" in r.json()["note"]


def test_listing_json_is_what_alpha_shows_never_more(pair):
    """beta's raw offer answer, read over the device link with the fleet key,
    has exactly the offered fields — never a path on beta's disk."""
    alpha, beta = pair["alpha"], pair["beta"]
    _join(alpha, beta)
    beta.http.post("/api/hub/text", json={"text": "x"})
    fkey = json.loads((beta.dir / "fleet.json").read_text())["key"]
    r = httpx.get(
        "http://%s:%d/api/hub/offer" % (beta.ip, beta.port),
        headers={"X-MindFlock-Remote": "alpha", "Authorization": "Bearer " + fkey},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["v"] == 1 and body["device"] == "beta"
    item = body["items"][0]
    assert set(item) == {
        "id",
        "name",
        "kind",
        "mime",
        "size",
        "sha256",
        "by",
        "by_name",
        "added",
        "note",
        "textual",
        "preview",
        "secret",
    }
