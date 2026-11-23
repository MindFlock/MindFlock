"""Shared files (the hub, renamed for people — core.hub, routes/hub): the
``file:`` code people and agents see now, the first ``hub:`` codes still read
everywhere, and ``POST /api/hub/batch`` — several items in one request,
answered per item (the app's selection bar).

Reuses test_hub's scratch world (a throwaway MINDFLOCK_HOME, stand-in
sessions with git worktrees, the typing step recorded)."""

from __future__ import annotations

import pytest

from backend.mcp import hub as mcp_hub
from backend.web.core import hub
from tests.unit.test_hub import _add_png, _add_text, client, world  # noqa: F401


# --------------------------------------------------------------------------- #
# the code
# --------------------------------------------------------------------------- #
def test_items_wear_a_file_code_and_old_hub_codes_still_resolve(world):
    rec = _add_png()
    assert rec["ref"] == "file:" + rec["id"]
    for raw in (rec["ref"], "hub:" + rec["id"], "HUB:" + rec["id"].upper(), rec["id"]):
        assert hub.get(raw)["id"] == rec["id"], raw
        assert client.get("/api/hub/items/" + raw).status_code == 200, raw
    # The MCP tool and the CLI take either prefix too.
    assert mcp_hub._quote_id("file:" + rec["id"]) == rec["id"]
    assert mcp_hub._quote_id("hub:" + rec["id"]) == rec["id"]
    from backend import cli_hub

    assert cli_hub._id("FILE:" + rec["id"]) == rec["id"]
    assert cli_hub._id("hub:" + rec["id"]) == rec["id"]


def test_both_prefixes_expand_in_a_send_once_each(world):
    rec = _add_png("login.png")
    r = client.post(
        "/api/instances/alpha/send",
        json={"text": "see file:%s and hub:%s" % (rec["id"], rec["id"])},
    )
    assert r.status_code == 200 and "hub_unknown" not in r.json()
    ((_t, text),) = world.typed
    assert text.count("MindFlock shared file") == 1 and rec["path"] in text
    assert text.startswith("see file:%s (MindFlock shared file: image " % rec["id"])
    # An old expansion (a prompt from before the rename, sent on again) is
    # never expanded a second time.
    old = 'hub:%s (MindFlock hub item: image "x", 1 bytes, at /x — …)' % rec["id"]
    assert hub.expand(old)[0] == old


def test_a_file_word_that_cannot_be_a_code_is_left_alone(world):
    # file:line, file:config … hold letters no code has: never a code, never
    # flagged; a word that could be one but matches nothing is flagged.
    for text in ("see file:line 12", "file:config", "a/file:abcd", "profile:abcd"):
        out, used, unknown = hub.expand(text)
        assert (out, used, unknown) == (text, [], []), text
    assert hub.unknown_refs("send file:zz99 now") == ["file:zz99"]
    assert "not in Shared files" in hub.unknown_note(["file:zz99"])
    # The first prefix keeps its first rule: every hub: word is a code.
    assert hub.unknown_refs("hub:nope") == ["hub:nope"]
    assert (
        hub.has_code("x FILE:abcd")
        and hub.has_code("hub:x")
        and not hub.has_code("files")
    )


def test_the_queue_drains_a_file_code_expanded(world, monkeypatch):
    import time

    from backend.web import server
    from backend.web.core import prompt_queue, queue_driver

    rec = _add_text("hello")
    client.post("/api/instances/alpha/queue", json={"text": "use " + rec["ref"]})
    item = prompt_queue.list_queue("alpha")[0]
    sent = []
    monkeypatch.setattr(
        server, "_send_to_agent", lambda n, t, submit=True: sent.append(t) or True
    )
    monkeypatch.setattr(queue_driver, "_dialog_on_screen", lambda i, n: False)
    queue_driver._send_queued_item("alpha", "mf_alpha", item, {}, time.time())
    assert sent and "MindFlock shared file" in sent[0] and rec["path"] in sent[0]


# --------------------------------------------------------------------------- #
# the batch
# --------------------------------------------------------------------------- #
def _batch(**body):
    return client.post("/api/hub/batch", json=body)


def test_batch_pin_unpin_answers_per_item_in_order(world):
    a, b = _add_png("a.png"), _add_text("b")
    r = _batch(action="pin", ids=[a["ref"], "hub:" + b["id"], "file:zz99", a["id"]])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["done"], body["failed"]) == (2, 1)
    ids = [x["id"] for x in body["results"]]
    assert ids == [a["id"], b["id"], "zz99"]  # a repeated item once
    assert (
        body["results"][2]["ok"] is False and body["results"][2]["code"] == "not_found"
    )
    assert hub.get(a["id"])["pinned"] and hub.get(b["id"])["pinned"]
    r = _batch(action="unpin", ids=[a["id"], b["id"]])
    assert r.json()["done"] == 2 and not hub.get(a["id"])["pinned"]


def test_batch_remove_and_restore_with_one_write_and_one_event(world, monkeypatch):
    a, b, c = _add_text("a"), _add_text("b"), _add_text("c")
    saves, events = [], []
    real_save = hub._save
    monkeypatch.setattr(hub, "_save", lambda doc: saves.append(1) or real_save(doc))
    monkeypatch.setattr(hub, "_changed", lambda: events.append(1))
    r = _batch(action="remove", ids=[a["id"], b["id"], "bad id!"])
    body = r.json()
    assert (
        (body["done"], body["failed"]) == (2, 1)
        and len(saves) == 1
        and len(events) == 1
    )
    assert (
        body["results"][2]["id"] == "bad id!"
        and body["results"][2]["code"] == "not_found"
    )
    listed = {i["id"] for i in client.get("/api/hub").json()["items"]}
    assert listed == {c["id"]}
    # Removing an item already removed says so (per item), changing nothing.
    r = _batch(action="remove", ids=[a["id"]])
    assert r.json()["results"][0]["code"] == "removed"
    r = _batch(action="restore", ids=[a["id"], b["id"], c["id"]])
    assert r.json()["done"] == 3
    assert {i["id"] for i in client.get("/api/hub").json()["items"]} == {
        a["id"],
        b["id"],
        c["id"],
    }


def test_batch_copy_lands_under_hub_never_over_a_file(world):
    a, b = _add_png("shot.png"), _add_text("notes", name="notes.txt")
    (world.wts["alpha"] / "hub").mkdir()
    (world.wts["alpha"] / "hub" / "shot.png").write_text("mine")
    r = _batch(action="copy", session="alpha", ids=[a["id"], b["id"], "file:zz99"])
    body = r.json()
    assert r.status_code == 200 and (body["done"], body["failed"]) == (2, 1)
    got = {x["id"]: x for x in body["results"]}
    assert got[a["id"]]["wanted"] == "hub/shot.png"
    assert got[a["id"]]["path"] != "hub/shot.png"  # the next free path
    assert (world.wts["alpha"] / "hub" / "shot.png").read_text() == "mine"
    assert (world.wts["alpha"] / got[a["id"]]["path"]).read_bytes()
    assert got[b["id"]]["path"].startswith("hub/")
    assert got["zz99"]["ok"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"action": "explode", "ids": ["abcd"]},
        {"action": "pin", "ids": []},
        {"action": "pin", "ids": "abcd"},
        {"action": "pin", "ids": [1]},
        {"action": "copy", "ids": ["abcd"]},
        {"action": "pin", "ids": ["abcd"], "session": "alpha"},
    ],
)
def test_batch_refuses_a_malformed_request(world, body):
    r = client.post("/api/hub/batch", json=body)
    assert r.status_code == 400 and r.json()["code"] == "bad_request"


def test_batch_copy_into_a_shared_folder_session_is_refused(world):
    world.inst["alpha"].PeerShare = "bob"
    rec = _add_text("x")
    r = _batch(action="copy", session="alpha", ids=[rec["id"]])
    assert r.status_code == 409


def test_batch_is_never_for_a_guest_or_a_relayed_request(world, monkeypatch):
    from backend.web.core import guests, remote

    rec = _add_text("x")
    r = client.post(
        "/api/hub/batch",
        json={"action": "remove", "ids": [rec["id"]]},
        headers={remote.REMOTE_HEADER: "other-box"},
    )
    assert r.status_code == 403
    guest = {"id": "g1", "sessions": ["alpha"]}
    assert not guests.path_allowed("POST", "/api/hub/batch", guest)
    monkeypatch.setattr(guests, "scope_guest", lambda req: guest)
    r = _batch(action="remove", ids=[rec["id"]])
    assert r.status_code == 403 and r.json()["code"] == "guest"
    assert hub.get(rec["id"])["id"] == rec["id"]
