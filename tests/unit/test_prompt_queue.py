"""Prompt queue store + send endpoint + drain loop (M-series).

Two user asks land here: a one-off "send a message to an agent window to start
it" (``POST /api/instances/{title}/send``) and a continuous prompt queue that a
background loop drains into idle agents so a run keeps rolling across usage
outages (``backend.web.core.prompt_queue`` + ``server._drain_one_queue``).

Hermetic: the queue store is pointed at a tmp file via
``MINDFLOCK_PROMPT_QUEUE_FILE``; tmux/agent I/O is monkeypatched, so nothing
real is spawned.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from fastapi.testclient import TestClient

from backend.session.storage import GitWorktreeData, InstanceData, Status
from backend.web.core import prompt_queue as pq


@pytest.fixture
def qfile(tmp_path, monkeypatch):
    """Point the queue store at an isolated tmp file."""
    p = tmp_path / "prompt_queues.json"
    monkeypatch.setenv("MINDFLOCK_PROMPT_QUEUE_FILE", str(p))
    return p


def _mk_inst(title, wt, *, status=Status.Running):
    from backend.session.instance import FromInstanceData

    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=120)
    data = InstanceData(
        title=title,
        path=wt,
        branch="b",
        status=status,
        created_at=t,
        updated_at=t,
        program="bash",
        worktree=GitWorktreeData(
            repo_path=wt,
            worktree_path=wt,
            session_name=title,
            branch_name="b",
        ),
    )
    return FromInstanceData(data, attach=False)


# --------------------------------------------------------------------------- #
# Store: enqueue / list / remove / clear / reorder / flags
# --------------------------------------------------------------------------- #
def test_enqueue_and_list(qfile):
    pq.enqueue("s", "first")
    pq.enqueue("s", "second")
    items = pq.list_queue("s")
    assert [i["text"] for i in items] == ["first", "second"]
    assert all(i["id"] for i in items)


def test_blank_prompt_ignored(qfile):
    pq.enqueue("s", "   ")
    assert pq.list_queue("s") == []


def test_remove_and_clear(qfile):
    pq.enqueue("s", "a")
    e = pq.enqueue("s", "b")
    bid = e["items"][1]["id"]
    pq.remove_item("s", bid)
    assert [i["text"] for i in pq.list_queue("s")] == ["a"]
    pq.clear("s")
    assert pq.list_queue("s") == []


def test_reorder(qfile):
    pq.enqueue("s", "a")
    e = pq.enqueue("s", "b")
    pq.move_item("s", e["items"][1]["id"], "up")
    assert [i["text"] for i in pq.list_queue("s")] == ["b", "a"]


def test_enqueue_at_index(qfile):
    pq.enqueue("s", "a")
    pq.enqueue("s", "c")
    pq.enqueue("s", "b", index=1)
    assert [i["text"] for i in pq.list_queue("s")] == ["a", "b", "c"]
    # index 0 = above everything; a huge index clamps to append.
    pq.enqueue("s", "front", index=0)
    pq.enqueue("s", "back", index=999)
    assert [i["text"] for i in pq.list_queue("s")] == ["front", "a", "b", "c", "back"]


def test_enqueue_many(qfile):
    pq.enqueue("s", "existing")
    entry, added, skipped = pq.enqueue_many("s", ["a", "  ", "b", None, "c"])
    assert (added, skipped) == (3, 0)
    assert [i["text"] for i in entry["items"]] == ["existing", "a", "b", "c"]
    assert entry["enabled"] is True
    # Ids are unique even though the rows share one timestamp.
    ids = [i["id"] for i in entry["items"]]
    assert len(set(ids)) == len(ids)


def test_enqueue_many_respects_cap(qfile):
    pq.enqueue_many("s", ["x%d" % i for i in range(pq._MAX_ITEMS - 1)])
    entry, added, skipped = pq.enqueue_many("s", ["a", "b", "c"])
    assert (added, skipped) == (1, 2)
    assert len(entry["items"]) == pq._MAX_ITEMS


def test_move_item_to(qfile):
    for t in ("a", "b", "c", "d"):
        pq.enqueue("s", t)
    items = pq.list_queue("s")
    # Drag "d" to the front, then "a" (now index 1) to the end (clamped).
    pq.move_item_to("s", items[3]["id"], 0)
    assert [i["text"] for i in pq.list_queue("s")] == ["d", "a", "b", "c"]
    pq.move_item_to("s", items[0]["id"], 99)
    assert [i["text"] for i in pq.list_queue("s")] == ["d", "b", "c", "a"]
    # Unknown id is a no-op.
    pq.move_item_to("s", "nope", 0)
    assert [i["text"] for i in pq.list_queue("s")] == ["d", "b", "c", "a"]


def test_update_item_text(qfile):
    e = pq.enqueue("s", "a")
    pq.enqueue("s", "b")
    aid = e["items"][0]["id"]
    pq.update_item("s", aid, "a — edited")
    items = pq.list_queue("s")
    assert [i["text"] for i in items] == ["a — edited", "b"]
    # The edit keeps the item's id (and therefore its place in line).
    assert items[0]["id"] == aid
    # Blank text and unknown ids leave the queue unchanged.
    pq.update_item("s", aid, "   ")
    pq.update_item("s", "nope", "x")
    assert [i["text"] for i in pq.list_queue("s")] == ["a — edited", "b"]


def test_flags_toggle(qfile):
    pq.enqueue("s", "a")
    pq.set_flags("s", enabled=False, loop=True)
    st = pq.get_state("s")
    assert st["enabled"] is False and st["loop"] is True


def test_wait_for_limit_defaults_true_and_toggles(qfile):
    pq.enqueue("s", "a")
    # Defaults on: hold + auto-resume across usage limits.
    assert pq.get_state("s")["wait_for_limit"] is True
    pq.set_flags("s", wait_for_limit=False)
    assert pq.get_state("s")["wait_for_limit"] is False
    # Toggling other flags leaves wait_for_limit untouched.
    pq.set_flags("s", loop=True)
    assert pq.get_state("s")["wait_for_limit"] is False


def test_loop_interval_defaults_zero_and_clamps(qfile):
    pq.enqueue("s", "a")
    assert pq.get_state("s")["loop_interval"] == 0
    pq.set_flags("s", loop_interval=15)
    assert pq.get_state("s")["loop_interval"] == 15
    # Negatives / junk clamp to 0 rather than raising.
    pq.set_flags("s", loop_interval=-4)
    assert pq.get_state("s")["loop_interval"] == 0
    pq.set_flags("s", loop_interval="nope")
    assert pq.get_state("s")["loop_interval"] == 0


def test_peek_next_respects_enabled(qfile):
    pq.enqueue("s", "a")
    assert pq.peek_next("s")["text"] == "a"
    pq.set_flags("s", enabled=False)
    assert pq.peek_next("s") is None


def test_record_sent_pops_without_loop(qfile):
    e = pq.enqueue("s", "a")
    pq.enqueue("s", "b")
    pq.record_sent("s", e["items"][0]["id"])
    assert [i["text"] for i in pq.list_queue("s")] == ["b"]


def test_record_sent_requeues_with_loop(qfile):
    pq.set_flags("s", loop=True)
    e = pq.enqueue("s", "only")
    pq.record_sent("s", e["items"][0]["id"])
    # Looped: the sent prompt is re-appended so it cycles forever.
    texts = [i["text"] for i in pq.list_queue("s")]
    assert texts == ["only"]
    assert pq.get_state("s")["last_text"] == "only"


def test_record_sent_pops_mid_queue_item(qfile):
    """Send-now can fire a NON-front item; record_sent pops it wherever it sits
    (front-of-queue is just the drain's special case)."""
    pq.enqueue("s", "a")
    e = pq.enqueue("s", "b")
    pq.enqueue("s", "c")
    pq.record_sent("s", e["items"][1]["id"])
    assert [i["text"] for i in pq.list_queue("s")] == ["a", "c"]
    assert pq.get_state("s")["last_text"] == "b"


def test_record_sent_mid_queue_requeues_with_loop(qfile):
    pq.set_flags("s", loop=True)
    pq.enqueue("s", "a")
    e = pq.enqueue("s", "b")
    pq.record_sent("s", e["items"][1]["id"])
    assert [i["text"] for i in pq.list_queue("s")] == ["a", "b"]


def test_prune_drops_dead_sessions(qfile):
    pq.enqueue("alive", "a")
    pq.enqueue("dead", "b")
    pq.prune(["alive"])
    assert pq.all_titles() == ["alive"]


def test_snapshot_shape(qfile):
    pq.enqueue("s", "a")
    snap = pq.snapshot()
    assert snap["s"]["items"][0]["text"] == "a"
    assert snap["s"]["enabled"] is True


def test_corrupt_file_reads_as_empty(qfile):
    qfile.write_text("{not json", encoding="utf-8")
    assert pq.list_queue("s") == []


# --------------------------------------------------------------------------- #
# /send + queue endpoints
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(qfile, tmp_path, monkeypatch):
    from backend.web import server

    inst = _mk_inst("t1", str(tmp_path / "wt"))
    monkeypatch.setitem(server.ENGINE.instances, "t1", inst)
    # Never spawn tmux: pretend the agent session is ready and capture sends.
    sent = []
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda i, t: ("agent_" + t, None)
    )
    monkeypatch.setattr(
        server,
        "_send_to_agent",
        lambda name, text, submit=True: (sent.append((name, text, submit)) or True),
    )
    c = TestClient(server.app)
    c._sent = sent
    yield c
    server.ENGINE.instances.pop("t1", None)


def test_send_endpoint_delivers_to_agent(client):
    r = client.post("/api/instances/t1/send", json={"text": "go build it"})
    assert r.status_code == 200 and r.json()["sent"] is True
    assert client._sent == [("agent_t1", "go build it", True)]


def test_send_rejects_empty(client):
    assert client.post("/api/instances/t1/send", json={"text": "  "}).status_code == 400


def test_send_unknown_instance_404(client):
    assert (
        client.post("/api/instances/nope/send", json={"text": "x"}).status_code == 404
    )


def test_queue_crud_via_api(client):
    assert (
        client.post("/api/instances/t1/queue", json={"text": "one"}).json()["pending"]
        == 1
    )
    client.post("/api/instances/t1/queue", json={"text": "two"})
    got = client.get("/api/instances/t1/queue").json()
    assert [i["text"] for i in got["items"]] == ["one", "two"]
    # flags
    f = client.post("/api/instances/t1/queue/flags", json={"loop": True}).json()
    assert f["loop"] is True
    # wait_for_limit flag round-trips (defaults True, toggles off).
    assert client.get("/api/instances/t1/queue").json()["wait_for_limit"] is True
    w = client.post(
        "/api/instances/t1/queue/flags", json={"wait_for_limit": False}
    ).json()
    assert w["wait_for_limit"] is False
    # loop_interval round-trips (defaults 0, set to minutes, junk clamps to 0).
    assert client.get("/api/instances/t1/queue").json()["loop_interval"] == 0
    assert (
        client.post("/api/instances/t1/queue/flags", json={"loop_interval": 10}).json()[
            "loop_interval"
        ]
        == 10
    )
    assert (
        client.post(
            "/api/instances/t1/queue/flags", json={"loop_interval": "x"}
        ).json()["loop_interval"]
        == 10
    )
    # clear
    assert client.request("DELETE", "/api/instances/t1/queue").json()["pending"] == 0


def test_queue_edit_via_api(client):
    st = client.post("/api/instances/t1/queue", json={"text": "one"}).json()
    iid = st["items"][0]["id"]
    r = client.post(
        "/api/instances/t1/queue/edit", json={"id": iid, "text": "one — edited"}
    )
    assert [i["text"] for i in r.json()["items"]] == ["one — edited"]
    # Empty replacement text is rejected, not applied.
    assert (
        client.post(
            "/api/instances/t1/queue/edit", json={"id": iid, "text": " "}
        ).status_code
        == 400
    )
    assert [
        i["text"] for i in client.get("/api/instances/t1/queue").json()["items"]
    ] == ["one — edited"]


def test_queue_insert_and_dnd_reorder_via_api(client):
    """The drag-and-drop shapes: POST /queue with an index inserts at that
    slot, and /queue/reorder with an index moves to an absolute position."""
    client.post("/api/instances/t1/queue", json={"text": "a"})
    client.post("/api/instances/t1/queue", json={"text": "c"})
    st = client.post("/api/instances/t1/queue", json={"text": "b", "index": 1}).json()
    assert [i["text"] for i in st["items"]] == ["a", "b", "c"]
    # Drag "c" to the front.
    cid = st["items"][2]["id"]
    r = client.post("/api/instances/t1/queue/reorder", json={"id": cid, "index": 0})
    assert [i["text"] for i in r.json()["items"]] == ["c", "a", "b"]
    # The one-slot nudge still works (older clients).
    r = client.post(
        "/api/instances/t1/queue/reorder", json={"id": cid, "direction": "down"}
    )
    assert [i["text"] for i in r.json()["items"]] == ["a", "c", "b"]
    # Junk index is a 400, not a crash or a silent reorder.
    assert (
        client.post(
            "/api/instances/t1/queue/reorder", json={"id": cid, "index": "x"}
        ).status_code
        == 400
    )


def test_queue_bulk_add_via_api(client):
    """The drop-a-CSV shape: ``texts`` bulk-appends and reports counts."""
    client.post("/api/instances/t1/queue", json={"text": "existing"})
    r = client.post("/api/instances/t1/queue", json={"texts": ["a", " ", "b"]}).json()
    assert (r["added"], r["skipped"]) == (2, 0)
    assert [i["text"] for i in r["items"]] == ["existing", "a", "b"]
    # All-blank payloads are a 400, same as an empty single text.
    assert (
        client.post("/api/instances/t1/queue", json={"texts": ["", "  "]}).status_code
        == 400
    )


def test_queue_send_now_delivers_and_pops(client):
    """Manual send-now: the queued item goes to the agent immediately and
    leaves the queue, even from a mid-queue position."""
    client.post("/api/instances/t1/queue", json={"text": "one"})
    st = client.post("/api/instances/t1/queue", json={"text": "two"}).json()
    iid = st["items"][1]["id"]
    r = client.post("/api/instances/t1/queue/send_now", json={"id": iid})
    assert r.status_code == 200
    assert client._sent == [("agent_t1", "two", True)]
    assert [i["text"] for i in r.json()["items"]] == ["one"]
    from backend.web import server

    server._QUEUE_STATE.pop("t1", None)


def test_queue_send_now_unknown_item_404(client):
    r = client.post("/api/instances/t1/queue/send_now", json={"id": "nope"})
    assert r.status_code == 404
    assert client._sent == []


def test_queue_send_now_requeues_with_loop(client):
    client.post("/api/instances/t1/queue/flags", json={"loop": True})
    st = client.post("/api/instances/t1/queue", json={"text": "cycle"}).json()
    r = client.post(
        "/api/instances/t1/queue/send_now", json={"id": st["items"][0]["id"]}
    )
    assert client._sent == [("agent_t1", "cycle", True)]
    assert [i["text"] for i in r.json()["items"]] == ["cycle"]  # re-appended
    from backend.web import server

    server._QUEUE_STATE.pop("t1", None)


def test_instances_payload_carries_queue_summary(client):
    client.post("/api/instances/t1/queue", json={"text": "one"})
    row = next(x for x in client.get("/api/instances").json() if x["title"] == "t1")
    q = row["queue"]
    assert q["pending"] == 1 and q["enabled"] is True
    # New fields power the Queue-tab badge's hold/auto-resume display.
    assert q["wait_for_limit"] is True and q["limited_until"] == 0.0


# --------------------------------------------------------------------------- #
# Drain loop decisions
# --------------------------------------------------------------------------- #
@pytest.fixture
def drain(qfile, tmp_path, monkeypatch):
    from backend.web import server

    inst = _mk_inst("d1", str(tmp_path / "wt"))
    monkeypatch.setitem(server.ENGINE.instances, "d1", inst)
    # Seed a record whose idle-dwell timer is already satisfied (idle_since far in
    # the past) so the send-path tests below exercise the send directly. The dwell
    # gate itself — a fresh idle must PERSIST before the first send — has its own
    # test (`test_drain_waits_for_idle_to_settle`), which pops this to start fresh.
    server._QUEUE_STATE["d1"] = {
        "armed": True,
        "sent_at": 0.0,
        "rebooted_at": 0.0,
        "idle_since": 1.0,
    }
    sent = []
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda i, t: ("agent_" + t, None)
    )
    monkeypatch.setattr(
        server,
        "_send_to_agent",
        lambda name, text, submit=True: (sent.append(text) or True),
    )
    yield server, sent
    server.ENGINE.instances.pop("d1", None)
    server._QUEUE_STATE.pop("d1", None)


def test_drain_sends_when_idle(drain, monkeypatch):
    server, sent = drain
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    pq.enqueue("d1", "task one")
    server._drain_one_queue("d1")
    assert sent == ["task one"]
    # Consumed (no loop) — queue now empty.
    assert pq.list_queue("d1") == []


def test_drain_waits_for_idle_to_settle(drain, monkeypatch):
    """A fresh idle must PERSIST for _QUEUE_IDLE_SETTLE before the first send, so a
    transient Stop between turns can't fire a queued prompt prematurely."""
    import time as _t

    server, sent = drain
    server._QUEUE_STATE.pop("d1", None)  # start with no dwell timer
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(_t, "time", lambda: clock["now"])
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    pq.enqueue("d1", "task one")

    server._drain_one_queue("d1")  # first idle tick: arm the dwell timer
    assert sent == []  # ...but hold — idle not yet settled
    assert [i["text"] for i in pq.list_queue("d1")] == ["task one"]

    clock["now"] += server._QUEUE_IDLE_SETTLE - 1  # still inside the settle window
    server._drain_one_queue("d1")
    assert sent == []  # still held

    clock["now"] += 2  # now past the settle window
    server._drain_one_queue("d1")
    assert sent == ["task one"]  # idle persisted -> sent
    assert pq.list_queue("d1") == []


def test_drain_resets_settle_when_agent_reworks(drain, monkeypatch):
    """A brief working blip mid-dwell resets the timer: the prompt must not slip
    through on stale idle-since accumulated across a working stretch."""
    import time as _t

    server, sent = drain
    server._QUEUE_STATE.pop("d1", None)
    clock = {"now": 1_000_000.0, "act": "idle"}
    monkeypatch.setattr(_t, "time", lambda: clock["now"])
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: clock["act"])
    pq.enqueue("d1", "task")

    server._drain_one_queue("d1")  # arm dwell timer at t0
    clock["act"] = "working"  # agent kicks off again
    clock["now"] += server._QUEUE_IDLE_SETTLE + 5  # time passes while working
    server._drain_one_queue("d1")  # working -> dwell timer cleared
    assert sent == []
    clock["act"] = "idle"
    server._drain_one_queue("d1")  # fresh idle -> timer restarts
    assert sent == []  # not sent despite earlier idle time


def test_drain_skips_while_working(drain, monkeypatch):
    server, sent = drain
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")
    pq.enqueue("d1", "task")
    server._drain_one_queue("d1")
    assert sent == []
    assert [i["text"] for i in pq.list_queue("d1")] == ["task"]


def test_drain_skips_while_clarify(drain, monkeypatch):
    server, sent = drain
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "clarify")
    pq.enqueue("d1", "task")
    server._drain_one_queue("d1")
    assert sent == []


def test_drain_holds_while_limit_window_closed(drain, monkeypatch):
    """activity=='limit' + window still closed -> hold: no Esc, no send, item
    stays queued (the UI shows the reset countdown; a later pass resumes)."""
    server, sent = drain
    escapes = []
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "limit")
    monkeypatch.setattr(server, "_refresh_limit_state", lambda i, t, n: 9_999_999_999.0)
    monkeypatch.setattr(server, "_send_escape_to_agent", lambda n: escapes.append(n))
    pq.enqueue("d1", "task")
    server._drain_one_queue("d1")
    assert sent == [] and escapes == []
    assert [i["text"] for i in pq.list_queue("d1")] == ["task"]


def test_drain_escapes_then_sends_when_limit_window_reopens(drain, monkeypatch):
    """The core fix: once the window reopens the queue dismisses the lingering
    limit menu with a single Esc, THEN submits the next prompt (so it lands on a
    clean input instead of selecting a menu entry)."""
    server, sent = drain
    order = []
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "limit")
    monkeypatch.setattr(server, "_refresh_limit_state", lambda i, t, n: 0.0)
    monkeypatch.setattr(server, "_send_escape_to_agent", lambda n: order.append("esc"))
    monkeypatch.setattr(
        server,
        "_send_to_agent",
        lambda name, text, submit=True: (order.append("send:" + text) or True),
    )
    pq.enqueue("d1", "resume task")
    server._drain_one_queue("d1")
    # Esc first, then the send — that ordering is what lets the prompt through.
    assert order == ["esc", "send:resume task"]
    assert pq.list_queue("d1") == []  # consumed


def test_drain_limit_resume_does_not_burn_queue_when_menu_persists(drain, monkeypatch):
    """Regression: if the Esc doesn't clear the menu (activity stays 'limit' and
    the meter still reads open), the drain must send at most ONCE and then wait —
    not pop+burn a fresh queued prompt into the limit screen every 8s pass."""
    server, sent = drain
    escapes = []
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "limit")
    monkeypatch.setattr(server, "_refresh_limit_state", lambda i, t, n: 0.0)
    monkeypatch.setattr(server, "_send_escape_to_agent", lambda n: escapes.append(n))
    pq.enqueue("d1", "p1")
    pq.enqueue("d1", "p2")
    pq.enqueue("d1", "p3")
    server._drain_one_queue("d1")  # armed -> escape + send p1, disarm
    server._drain_one_queue("d1")  # still 'limit', disarmed -> no further send
    server._drain_one_queue("d1")
    assert sent == ["p1"]  # exactly one prompt sent, not the whole queue
    assert len(escapes) == 1
    assert [i["text"] for i in pq.list_queue("d1")] == ["p2", "p3"]


def test_drain_stops_queue_on_limit_when_wait_off(drain, monkeypatch):
    """wait_for_limit off: a limit stops the queue (auto-run flips off) instead
    of waiting — no Esc, no send, and enabled goes False for a manual resume."""
    server, sent = drain
    escapes = []
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "limit")
    monkeypatch.setattr(server, "_send_escape_to_agent", lambda n: escapes.append(n))
    pq.enqueue("d1", "task")
    pq.set_flags("d1", wait_for_limit=False)
    server._drain_one_queue("d1")
    assert sent == [] and escapes == []
    assert server._prompt_queue.get_state("d1")["enabled"] is False
    assert [i["text"] for i in pq.list_queue("d1")] == ["task"]  # kept for resume


def test_drain_does_not_double_send_until_reworks(drain, monkeypatch):
    server, sent = drain
    state = {"act": "idle"}
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: state["act"])
    pq.set_flags("d1", loop=True)
    pq.enqueue("d1", "loop task")
    server._drain_one_queue("d1")  # sends once, disarms
    server._drain_one_queue("d1")  # still idle, disarmed -> no send
    assert sent == ["loop task"]
    state["act"] = "working"  # agent picked it up -> re-arm (resets dwell)
    server._drain_one_queue("d1")
    state["act"] = "idle"
    server._QUEUE_STATE["d1"]["sent_at"] = 0.0  # skip the send cooldown for the test
    server._QUEUE_STATE["d1"]["idle_since"] = 1.0  # and treat idle as already settled
    server._drain_one_queue("d1")  # armed again -> sends the requeued prompt
    assert sent == ["loop task", "loop task"]


def test_drain_reboots_offline_session(drain, monkeypatch):
    server, sent = drain
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "offline")
    rebooted = []
    monkeypatch.setattr(
        server,
        "_ensure_agent_session",
        lambda i, t: (rebooted.append(t) or ("agent_" + t, None)),
    )
    pq.enqueue("d1", "task")
    server._drain_one_queue("d1")
    assert rebooted == ["d1"] and sent == []  # rebooted, not yet sent
    # A second immediate pass is rate-limited (no reboot storm).
    server._drain_one_queue("d1")
    assert rebooted == ["d1"]


def test_drain_disabled_queue_is_noop(drain, monkeypatch):
    server, sent = drain
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    pq.enqueue("d1", "task")
    pq.set_flags("d1", enabled=False)
    server._drain_one_queue("d1")
    assert sent == []


def test_drain_holds_when_limited_and_wait_on(drain, monkeypatch):
    """wait_for_limit on (default): a usage limit holds the queue — nothing is
    sent and the queue stays enabled so a later pass resumes it."""
    import time as _t

    server, sent = drain
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    monkeypatch.setattr(
        server, "_refresh_limit_state", lambda i, t, n: _t.time() + 3600
    )
    pq.enqueue("d1", "task")
    server._drain_one_queue("d1")
    assert sent == []
    st = pq.get_state("d1")
    assert st["enabled"] is True  # still armed to resume
    assert [i["text"] for i in st["items"]] == ["task"]


def test_drain_stops_when_limited_and_wait_off(drain, monkeypatch):
    """wait_for_limit off: hitting a usage limit flips auto-run off (the queue
    stops) instead of holding — the prompt stays queued for a manual resume."""
    import time as _t

    server, sent = drain
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    monkeypatch.setattr(
        server, "_refresh_limit_state", lambda i, t, n: _t.time() + 3600
    )
    pq.enqueue("d1", "task")
    pq.set_flags("d1", wait_for_limit=False)
    server._drain_one_queue("d1")
    assert sent == []
    st = pq.get_state("d1")
    assert st["enabled"] is False  # queue stopped
    assert [i["text"] for i in st["items"]] == ["task"]  # prompt preserved


def test_drain_loop_timer_spaces_out_sends(drain, monkeypatch):
    """loop + loop_interval: the first send is immediate, but the looped prompt
    only re-sends once the interval has elapsed."""
    import time as _time

    server, sent = drain
    clock = {"now": 1_000_000.0, "act": "idle"}
    monkeypatch.setattr(_time, "time", lambda: clock["now"])
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: clock["act"])
    pq.set_flags("d1", loop=True, loop_interval=5)  # every 5 minutes
    pq.enqueue("d1", "cycle")

    server._drain_one_queue("d1")  # first send: immediate
    assert sent == ["cycle"]

    clock["act"] = "working"
    server._drain_one_queue("d1")  # agent picks it up -> re-arm (resets dwell)
    clock["act"] = "idle"
    server._QUEUE_STATE["d1"]["sent_at"] = 0.0  # isolate from the 8s cooldown
    server._QUEUE_STATE["d1"]["idle_since"] = clock["now"] - (
        server._QUEUE_IDLE_SETTLE + 1
    )  # idle already settled, so the hold below is the 5-min interval, not the dwell
    server._drain_one_queue("d1")  # within the 5-min window -> held
    assert sent == ["cycle"]

    clock["now"] += 5 * 60  # 5 minutes later
    server._QUEUE_STATE["d1"]["sent_at"] = 0.0
    server._drain_one_queue("d1")  # timer elapsed -> sends again
    assert sent == ["cycle", "cycle"]


def test_drain_rearms_after_long_idle_dud_send(drain, monkeypatch):
    """A send that never started a turn (it landed on a usage-limit screen)
    leaves ``armed`` False with no "working" transition coming. Once the agent
    has sat idle for _QUEUE_REARM_IDLE past that send, the drain must re-arm
    and retry — otherwise the queue is dead until a human pokes the session."""
    import time as _time

    server, sent = drain
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(_time, "time", lambda: clock["now"])
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    pq.enqueue("d1", "task")
    # Simulate the aftermath of a dud send: disarmed, never seen working.
    server._QUEUE_STATE["d1"] = {
        "armed": False,
        "sent_at": clock["now"] - 30.0,
        "rebooted_at": 0.0,
        "idle_since": 1.0,
    }
    server._drain_one_queue("d1")
    assert sent == []  # too soon — a real turn may just be slow to register

    clock["now"] += server._QUEUE_REARM_IDLE
    server._drain_one_queue("d1")
    assert sent == ["task"]  # re-armed and retried
