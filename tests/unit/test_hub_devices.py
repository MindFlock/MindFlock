"""Shared files across the person's devices (core.hub_remote, the "offered"
routes of routes/hub.py, the forwarding allow-list in core.remote).

The other device is FAKED behind the real forwarding middleware: its
``_proxy_http`` (the one network hop) is replaced by :class:`FakeDevice`,
which answers like an honest MindFlock — or, per test, like a hostile or
broken one. Everything above that hop is the real code: the allow-list, the
one-hop rule, the lend rule, the listing's cleaning, the store's add. The
two-real-servers version is tests/integration/test_hub_devices_e2e.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse

import pytest

from backend.web import server
from backend.web.core import hub, hub_remote, remote
from backend.web.core.send_session import Refusal
from tests.unit.test_hub import (
    K,
    PNG,
    _add_png,
    _add_text,
    _box,
    _set,
    client,
    world,
)  # noqa: F401

DEV = "macmini"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class FakeDevice:
    """Another of the person's devices, answering the forwarded hub GETs.

    ``items``: id -> {name, kind, data, ...} it offers (an honest listing is
    built from them); ``routes``: path prefix -> fn(path, query) ->
    (status, content type, [chunks]) to answer something else."""

    def __init__(self) -> None:
        self.items: dict = {}
        self.routes: dict = {}
        self.calls: list = []

    def offer(
        self, iid: str, data: bytes, name: str = "a.txt", kind: str = "file", **kw
    ):
        self.items[iid] = {"name": name, "kind": kind, "data": data, **kw}
        return iid

    def listing(self) -> dict:
        out = []
        for iid, it in self.items.items():
            out.append(
                {
                    "id": iid,
                    "name": it["name"],
                    "kind": it["kind"],
                    "mime": it.get(
                        "mime", "image/png" if it["kind"] == "image" else ""
                    ),
                    "size": it.get("size", len(it["data"])),
                    "sha256": it.get("sha256", _sha(it["data"])),
                    "by": "you",
                    "by_name": "",
                    "added": time.time(),
                    "note": it.get("note", ""),
                    "textual": it["kind"] == "text",
                    "preview": it.get("preview", ""),
                    "secret": it.get("secret", False),
                }
            )
        return {"v": 1, "device": DEV, "items": out}

    def answer(self, url: str):
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((u.path, q))
        for prefix, fn in self.routes.items():
            if u.path.startswith(prefix):
                return fn(u.path, q)
        if u.path == "/api/hub/offer":
            return 200, "application/json", [json.dumps(self.listing()).encode()]
        parts = u.path.split("/")
        if u.path.startswith("/api/hub/offer/") and len(parts) == 6:
            it = self.items.get(parts[4])
            if it is None:
                return 404, "application/json", [b'{"error":"x","code":"not_offered"}']
            if parts[5] == "raw":
                off, n = int(q["offset"]), int(q["length"])
                return 200, "application/octet-stream", [it["data"][off : off + n]]
            if parts[5] == "thumb":
                return 200, "image/png", [it["data"]]
        return 404, "application/json", [b'{"detail":"Not Found"}']


@pytest.fixture
def dev(world, monkeypatch):
    """A connected device ``macmini``, paired with this one (a token), its
    network hop answered by a :class:`FakeDevice`."""
    fake = FakeDevice()
    state = {
        "key": DEV,
        "host": "Mac mini",
        "os": "macos",
        "ip": "100.64.0.7",
        "ips": ["100.64.0.7"],
        "dns": DEV + ".example.ts.net",
        "version": "0.15.0",
        "reachable": True,
        "remote_control": True,
        "auth": True,
        "base_url": "http://fake-macmini",
        "instances": [],
        "last_seen": time.time(),
    }
    monkeypatch.setitem(remote._DEVICES, DEV, state)
    monkeypatch.setattr(remote, "token_for", lambda k: "paired" if k == DEV else "")
    if remote.aiohttp is None:  # pragma: no cover — the web group has it
        monkeypatch.setattr(remote, "aiohttp", object())

    async def hop(self, scope, receive, send, dev, url, timeout=60.0, lend=False):
        fake.lends.append(lend)
        status, ctype, chunks = fake.answer(url)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", ctype.encode())],
            }
        )
        for c in chunks:
            await send({"type": "http.response.body", "body": c, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    fake.lends = []
    monkeypatch.setattr(remote.RemoteProxyMiddleware, "_proxy_http", hop)
    real_lend = remote._may_lend_credential

    async def lend(scope):
        # The TestClient stands for the owner at this machine; any other
        # scope gets the real rule.
        if (scope.get("client") or ("",))[0] == "testclient":
            return True
        return await real_lend(scope)

    monkeypatch.setattr(remote, "_may_lend_credential", lend)
    monkeypatch.setattr(hub, "self_device", lambda: "laptop")
    hub_remote.forget()
    hub_remote._THUMBS.clear()
    hub.REMOTE_IDS.clear()
    yield fake
    hub_remote.forget()
    hub_remote._THUMBS.clear()
    hub.REMOTE_IDS.clear()


def _devices(fresh=1) -> dict:
    r = client.get("/api/hub/devices", params={"fresh": fresh})
    assert r.status_code == 200, r.text
    return {d["device"]: d for d in r.json()["devices"]}


def _fetch(code, **kw):
    return client.post("/api/hub/devices/fetch", json={"id": code, **kw})


def _incoming_empty(world) -> bool:
    inc = world.home / "hub" / hub.INCOMING
    return not inc.exists() or not os.listdir(inc)


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #
def test_the_other_device_is_listed_with_its_items_and_nothing_is_copied(dev, world):
    dev.offer("k7m2", PNG, "shot.png", "image")
    dev.offer("p3qx", b"hello\n", "note.txt", "text", preview="hello")
    d = _devices()[DEV]
    assert d["state"] == "ok" and d["name"] == "Mac mini"
    rows = {it["id"]: it for it in d["items"]}
    assert rows["k7m2"]["ref"] == "file:k7m2" and rows["k7m2"]["copy"] is None
    assert rows["k7m2"]["device_name"] == "Mac mini"
    assert rows["p3qx"]["preview"] == "hello"
    assert client.get("/api/hub").json()["items"] == []
    # Only the list was asked; no bytes moved.
    assert [c[0] for c in dev.calls] == ["/api/hub/offer"]


def test_a_code_two_devices_hold_shows_the_device_form(dev, world):
    mine = _add_text("mine")
    dev.offer(mine["id"], b"theirs", "t.txt")
    row = _devices()[DEV]["items"][0]
    assert row["ref"] == "file@%s:%s" % (DEV, mine["id"])


def test_a_new_id_avoids_the_ids_other_devices_hold(dev, world, monkeypatch):
    dev.offer("k7m2", b"x")
    _devices()
    assert "k7m2" in hub.REMOTE_IDS
    picks = iter(["k", "7", "m", "2", "a", "b", "c", "d"])
    monkeypatch.setattr(hub.secrets, "choice", lambda alphabet: next(picks))
    rec = _add_text("new one")
    assert rec["id"] == "abcd"


@pytest.mark.parametrize(
    "status,body,state",
    [
        (404, b'{"detail":"Not Found"}', "old"),
        (405, b'{"detail":"Method Not Allowed"}', "old"),
        (403, b'{"error":"remote control is disabled on this device"}', "off"),
        (403, b'{"error":"x","code":"not_offered"}', "private"),
        (401, b'{"error":"x","code":"not_linked"}', "not_linked"),
        (502, b'{"error":"device unreachable"}', "offline"),
        (500, b"<html>oops</html>", "error"),
        (200, b"not json", "error"),
        (200, b'{"items": "nope"}', "error"),
    ],
)
def test_a_device_that_cannot_answer_is_named_never_an_empty_list(
    dev, status, body, state
):
    dev.routes["/api/hub/offer"] = lambda p, q: (status, "application/json", [body])
    d = _devices()[DEV]
    assert d["state"] == state and d["items"] == []
    assert d["note"] and "Mac mini" in d["note"]
    if state == "old":
        assert "update" in d["note"]


def test_an_offline_device_is_named_offline(dev):
    remote._DEVICES[DEV]["reachable"] = False
    d = _devices()[DEV]
    assert d["state"] == "offline" and "offline" in d["note"]
    assert dev.calls == []


def test_a_roster_member_never_seen_is_named_offline(dev, monkeypatch):
    from backend.web.core import fleet

    monkeypatch.setattr(fleet, "in_fleet", lambda: True)
    monkeypatch.setattr(
        fleet, "live_members", lambda: {"winrig": {"host": "Win rig"}, "laptop": {}}
    )
    d = _devices()
    assert d["winrig"]["state"] == "offline" and d["winrig"]["name"] == "Win rig"


def test_a_stranger_on_the_tailnet_is_never_listed(dev, monkeypatch):
    # Connected (gate off, remote control on) but neither paired nor a member.
    monkeypatch.setattr(remote, "token_for", lambda k: "")
    remote._DEVICES[DEV]["auth"] = False
    assert DEV not in _devices()
    r = _fetch("file@%s:k7m2" % DEV)
    assert r.status_code == 404 and r.json()["code"] == "no_device"


def test_the_listing_is_cached_then_asked_again(dev):
    _devices(fresh=0)
    _devices(fresh=0)
    assert len(dev.calls) == 1
    _devices(fresh=1)
    assert len(dev.calls) == 2


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
def test_fetch_lands_here_verified_and_a_second_use_does_not_fetch(dev, world):
    data = os.urandom(9 * 1024 * 1024 + 17)  # three ranged requests
    dev.offer("k7m2", data, "dump.bin")
    r = _fetch("file@%s:k7m2" % DEV)
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["already"] is False and rec["from"] == "Mac mini"
    assert rec["id"] == "k7m2" and rec["sha256"] == _sha(data)
    assert rec["origin"] == {
        "device": DEV,
        "name": "Mac mini",
        "id": "k7m2",
        "sha256": _sha(data),
        "fetched": rec["origin"]["fetched"],
    }
    assert open(rec["path"], "rb").read() == data
    assert rec["path"].startswith(str(world.home / "hub"))
    raws = [c for c in dev.calls if c[0].endswith("/raw")]
    assert [int(q["offset"]) for _p, q in raws] == [0, 4 << 20, 8 << 20]
    assert all(q["sha256"] == _sha(data) for _p, q in raws)
    n = len(dev.calls)
    again = _fetch("file@%s:k7m2" % DEV).json()
    assert again["already"] is True and again["id"] == rec["id"]
    assert not any(c[0].endswith("/raw") for c in dev.calls[n:])
    assert len(client.get("/api/hub").json()["items"]) == 1
    assert _devices()[DEV]["items"][0]["copy"] == rec["id"]


def test_a_taken_code_gets_a_new_one_here(dev, world):
    mine = _add_text("mine")
    dev.offer(mine["id"], b"theirs")
    rec = _fetch("file@%s:%s" % (DEV, mine["id"])).json()
    assert rec["id"] != mine["id"] and rec["origin"]["id"] == mine["id"]


def test_the_origin_changing_its_bytes_fetches_a_new_copy(dev, world):
    dev.offer("k7m2", b"first")
    one = _fetch("file@%s:k7m2" % DEV).json()
    dev.offer("k7m2", b"second, different")
    hub_remote.forget()
    two = _fetch("file@%s:k7m2" % DEV).json()
    assert two["already"] is False and two["id"] != one["id"]
    assert open(one["path"], "rb").read() == b"first"


def test_the_origin_removing_it_leaves_the_copy(dev, world):
    dev.offer("k7m2", b"keep me")
    rec = _fetch("file@%s:k7m2" % DEV).json()
    dev.items.clear()
    hub_remote.forget()
    assert hub.get(rec["id"])["origin"]["device"] == DEV
    out = client.post("/api/hub/expand", json={"text": "file:k7m2"}).json()
    assert out["unknown"] == [] and rec["path"] in out["text"]


def test_concurrent_uses_fetch_once(dev, world):
    import asyncio

    dev.offer("k7m2", b"once")

    async def both():
        return await asyncio.gather(
            hub_remote.fetch({"type": "http", "headers": []}, DEV, "k7m2"),
            hub_remote.fetch({"type": "http", "headers": []}, DEV, "k7m2"),
        )

    a, b = asyncio.run(both())
    assert a["id"] == b["id"]
    assert len([c for c in dev.calls if c[0].endswith("/raw")]) == 1
    assert len(hub.list_items()["items"]) == 1


def test_the_local_add_rules_hold_for_a_fetch(dev, world):
    _set(item_mb=1)
    dev.offer("bigg", b"x" * (2 * 1024 * 1024))
    r = _fetch("file@%s:bigg" % DEV)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    assert not any(c[0].endswith("/raw") for c in dev.calls)
    _set(item_mb=None, max_items=1)
    _add_text("fills it")
    dev.offer("smal", b"y")
    r = _fetch("file@%s:smal" % DEV)
    assert r.status_code == 507 and r.json()["code"] == "hub_full"


# --------------------------------------------------------------------------- #
# codes typed here
# --------------------------------------------------------------------------- #
def test_a_send_with_another_devices_code_fetches_then_expands(dev, world):
    dev.offer("k7m2", b"crash log\n", "crash.log")
    r = client.post("/api/instances/alpha/send", json={"text": "look at file:k7m2"})
    assert r.status_code == 200, r.text
    assert "hub_unknown" not in r.json()
    sent = world.typed[-1][1]
    assert "MindFlock shared file" in sent and str(world.home / "hub") in sent
    assert hub.get("k7m2")["origin"]["device"] == DEV


def test_own_item_always_wins_and_nothing_is_fetched(dev, world):
    mine = _add_text("mine")
    dev.offer(mine["id"], b"theirs")
    out = client.post("/api/hub/expand", json={"text": mine["ref"]}).json()
    assert mine["path"] in out["text"]
    assert dev.calls == []


def test_two_holders_is_a_plain_sentence_never_a_guess(dev, world, monkeypatch):
    dev.offer("k7m2", b"one")
    other = dict(remote._DEVICES[DEV], key="winrig", host="Win rig")
    monkeypatch.setitem(remote._DEVICES, "winrig", other)
    monkeypatch.setattr(remote, "token_for", lambda k: "paired")
    out = client.post("/api/hub/expand", json={"text": "see file:k7m2"}).json()
    assert out["unknown"] == ["file:k7m2"] and out["text"] == "see file:k7m2"
    assert "file@macmini:k7m2" in out["note"] and "file@winrig:k7m2" in out["note"]
    assert hub.list_items()["items"] == []
    out = client.post("/api/hub/expand", json={"text": "file@winrig:k7m2"}).json()
    assert out["unknown"] == [] and "MindFlock shared file" in out["text"]


def test_a_code_for_an_unreachable_device_says_so(dev, world):
    remote._DEVICES[DEV]["reachable"] = False
    out = client.post("/api/hub/expand", json={"text": "file:k7m2"}).json()
    assert out["unknown"] == ["file:k7m2"] and "Mac mini" in out["note"]
    out = client.post("/api/hub/expand", json={"text": "file@macmini:k7m2"}).json()
    assert out["unknown"] == ["file@macmini:k7m2"] and "Mac mini" in out["note"]


def test_a_device_code_older_versions_never_read_as_their_own():
    # The rule an older MindFlock still runs (its HANDLE_RE): no part of a
    # device code is a code to it.
    import re

    old = re.compile(
        r"(?<![A-Za-z0-9_./:@-])(file|hub):([A-Za-z0-9]{4,8})(?![A-Za-z0-9_])"
    )
    assert not old.search("see file@mac-mini:k7m2 and hub@desk:abcd")


def test_the_queue_fetches_while_the_owner_is_there(dev, world):
    dev.offer("k7m2", b"for later")
    r = client.post("/api/instances/alpha/queue", json={"text": "later: file:k7m2"})
    assert r.status_code == 200, r.text
    assert "hub_unknown" not in r.json()
    assert hub.get("k7m2")["origin"]["device"] == DEV


# --------------------------------------------------------------------------- #
# a mixed selection
# --------------------------------------------------------------------------- #
def test_a_mixed_selection_copies_what_it_can_and_says_why_not(dev, world):
    mine = _add_text("mine")
    dev.offer("k7m2", b"theirs", "theirs.txt")
    dev.offer("secr", b"A=1", ".env")
    r = client.post(
        "/api/hub/batch",
        json={
            "action": "copy",
            "session": "alpha",
            "ids": [
                mine["ref"],
                "file@macmini:k7m2",
                "file@macmini:secr",
                "file@macmini:gone",
            ],
        },
    )
    assert r.status_code == 200, r.text
    res = r.json()["results"]
    assert [x["ok"] for x in res] == [True, True, False, False]
    assert res[2]["code"] == "secret" and res[3]["code"] == "not_found"
    assert (world.wts["alpha"] / "hub" / "theirs.txt").read_bytes() == b"theirs"
    assert r.json()["done"] == 2 and r.json()["failed"] == 2
    r = client.post(
        "/api/hub/batch", json={"action": "remove", "ids": ["file@macmini:k7m2"]}
    )
    assert r.json()["results"][0]["code"] == "read_only"
    r = client.post(
        "/api/hub/batch",
        json={"action": "fetch", "ids": [mine["ref"], "file@macmini:k7m2"]},
    )
    assert [x["already"] for x in r.json()["results"]] == [True, True]


# --------------------------------------------------------------------------- #
# thumbnails
# --------------------------------------------------------------------------- #
def test_a_small_picture_is_its_own_thumbnail_checked_here(dev, world):
    dev.offer("k7m2", PNG, "shot.png", "image")
    r = client.get("/api/hub/devices/macmini/items/k7m2/thumb")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
    n = len(dev.calls)
    client.get("/api/hub/devices/macmini/items/k7m2/thumb")
    assert len(dev.calls) == n  # cached
    assert hub.list_items()["items"] == []  # a thumbnail is not a fetch


def test_a_big_picture_has_no_thumbnail_and_nothing_is_pulled(dev, world):
    dev.offer("k7m2", PNG + b"\0" * (hub_remote.THUMB_MAX + 1), "big.png", "image")
    r = client.get("/api/hub/devices/macmini/items/k7m2/thumb")
    assert r.status_code == 404 and r.json()["code"] == "no_thumb"
    assert not any("/thumb" in c[0] or "/raw" in c[0] for c in dev.calls)


# --------------------------------------------------------------------------- #
# agents
# --------------------------------------------------------------------------- #
def test_an_agent_fetches_with_its_own_key_and_it_is_logged(dev, world, monkeypatch):
    from backend import log

    lines = []

    class _L:
        def Printf(self, fmt, *a):
            lines.append(fmt % a)

    monkeypatch.setattr(log, "InfoLog", _L())
    dev.offer("k7m2", b"for the agent", "a.txt")
    r = client.post("/api/instances/alpha/hub/fetch", json={"id": "file@macmini:k7m2"})
    assert r.status_code == 403 and r.json()["code"] == "not_this_session"
    r = client.post(
        "/api/instances/alpha/hub/fetch", json={"id": "file:k7m2", "key": K("alpha")}
    )
    assert r.status_code == 200, r.text
    assert any('session "alpha"' in ln and "macmini" in ln for ln in lines)


def test_an_agent_never_brings_a_secret(dev, world):
    dev.offer("secr", b"A=1", ".env")
    r = client.post(
        "/api/instances/alpha/hub/fetch",
        json={"id": "file@macmini:secr", "key": K("alpha"), "secret_ok": True},
    )
    assert r.status_code == 403 and r.json()["code"] == "secret"
    assert r.json()["can_confirm"] is False
    assert hub.list_items()["items"] == []


def test_the_mcp_lists_other_devices_and_gets_through_a_fetch(dev, world):
    dev.offer("k7m2", b"remote words\n", "w.txt", "text")
    box = _box("alpha")
    lst = box.hub({"action": "list"}, None)
    assert lst["devices"][0]["device"] == DEV
    row = lst["devices"][0]["items"][0]
    assert row["ref"] == "file:k7m2" and row["on"] == "Mac mini"
    got = box.hub({"action": "get", "id": "file@macmini:k7m2"}, None)
    assert got["text"] == "remote words\n" and got["from"] == "Mac mini"
    took = box.hub({"action": "take", "id": "file@macmini:k7m2"}, None)
    assert (world.wts["alpha"] / took["path"]).read_bytes() == b"remote words\n"


def test_a_peer_session_gets_nothing_from_other_devices(dev, world, monkeypatch):
    from tests.unit.test_hub import _Inst

    world.inst["alpha"] = _Inst(world.wts["alpha"], "alpha", peer="pal")
    dev.offer("k7m2", b"x")
    r = client.post(
        "/api/instances/alpha/hub/fetch", json={"id": "file:k7m2", "key": K("alpha")}
    )
    assert r.status_code in (403, 409), r.text
    r = client.post("/api/instances/alpha/send", json={"text": "file:k7m2"})
    assert dev.calls == [] and hub.list_items()["items"] == []


def test_the_mcp_outside_a_session_says_not_found_for_a_typo(dev, world):
    box = _box(None)
    with pytest.raises(Exception) as err:
        box.hub({"action": "get", "id": "file:zzzz"}, None)
    assert "zzzz" in str(err.value) and "inside a MindFlock session" not in str(
        err.value
    )
