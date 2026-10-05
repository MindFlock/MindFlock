"""Inter-agent message routes + the mailbox delivery lane (``web.server``).

Every tmux touch is faked: ``_send_to_agent`` records what would be typed,
``_live_session_name`` decides whether the agent's tmux session "exists", and
``_agent_activity`` is the uncached probe the lane and ``now`` consult.
``_ensure_agent_session`` (the reboot path) and ``_note_human_input`` raise or
record so a test fails if the mailbox ever boots an agent or stamps a human.
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.session.storage import GitWorktreeData, InstanceData, Status
from backend.web.core import agent_state
from backend.web.core import mailbox as mb
from backend.web.core import prompt_queue as pq


def _mk_inst(title, wt, *, status=Status.Running, program="claude"):
    from backend.session.instance import FromInstanceData

    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=120)
    data = InstanceData(
        title=title,
        path=wt,
        branch="b",
        status=status,
        created_at=t,
        updated_at=t,
        program=program,
        worktree=GitWorktreeData(
            repo_path=wt,
            worktree_path=wt,
            session_name=title,
            branch_name="b",
        ),
    )
    return FromInstanceData(data, attach=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Three live sessions — ``orch`` (parent of ``w1``), ``w1``, ``other`` —
    on an empty engine, with every tmux/probe collaborator faked."""
    from backend.web import server

    monkeypatch.setenv("MINDFLOCK_PROMPT_QUEUE_FILE", str(tmp_path / "pq.json"))
    monkeypatch.setattr(server.ENGINE, "instances", {})
    insts = {}
    for title, program in (("orch", "claude"), ("w1", "claude"), ("other", "codex")):
        inst = _mk_inst(title, str(tmp_path / title), program=program)
        server.ENGINE.instances[title] = inst
        insts[title] = inst
    insts["w1"].Parent = "orch"

    state = SimpleNamespace(
        typed=[],
        activity={"orch": "idle", "w1": "idle", "other": "idle"},
        live={"orch", "w1", "other"},
        humans=[],
        fg={},  # title -> pane foreground command (default "claude")
        agent_child=False,  # a live non-shell process under a bare-shell pane
        procs={},  # title -> extra command lines in the pane's process tree
        tmux_typing=set(),  # titles with recent raw tmux-client input
        send_ok=True,
        events=[],
        insts=insts,
    )

    def fake_send(name, text, submit=True):
        if not state.send_ok:
            return False
        state.typed.append((name, text, submit))
        return True

    def no_boot(inst, title):
        raise AssertionError("the mailbox must never (re)boot an agent")

    monkeypatch.setattr(server, "_send_to_agent", fake_send)
    monkeypatch.setattr(
        server, "_live_session_name", lambda n: n if _strip(n) in state.live else None
    )
    monkeypatch.setattr(
        server, "_agent_activity", lambda inst, title: state.activity[title]
    )
    monkeypatch.setattr(server, "_ensure_agent_session", no_boot)
    monkeypatch.setattr(server, "_note_human_input", lambda t: state.humans.append(t))
    monkeypatch.setattr(server, "_refresh_limit_state", lambda i, t, n: 0.0)
    monkeypatch.setattr(
        server,
        "_pane_meta",
        lambda n: (state.fg.get(_strip(n), "claude"), 1.0, "pid:" + _strip(n), "80x24"),
    )
    monkeypatch.setattr(
        server, "_pane_has_agent_process", lambda pid: state.agent_child
    )

    def fake_runs_agent(pid, names):
        # The pane's process tree as command lines: the foreground, the agent
        # underneath a bare-shell wrapper when ``agent_child``, plus extras —
        # judged by the REAL argv matcher.
        title = str(pid).split(":", 1)[-1]
        tree = [state.fg.get(title, "claude")]
        if state.agent_child:
            tree.append("claude --continue")
        tree += state.procs.get(title, [])
        return any(agent_state._argv_names_agent(a, names) for a in tree)

    monkeypatch.setattr(server._agent_state, "_pane_runs_agent", fake_runs_agent)
    monkeypatch.setattr(
        server, "_tmux_client_input_recent", lambda t, w: t in state.tmux_typing
    )
    monkeypatch.setattr(server, "_HUMAN_INPUT_AT", {})
    for t in list(server._MAIL_STATE):
        server._MAIL_STATE.pop(t, None)
    for t in ("orch", "w1", "other"):
        server._QUEUE_STATE.pop(t, None)
    mb._WAITERS.clear()
    unsub = server._events.BUS.subscribe(
        lambda e: state.events.append(e) if e["event"] == "session.message" else None
    )
    state.client = TestClient(server.app)
    yield server, state
    unsub()
    mb._WAITERS.clear()
    for t in list(server._MAIL_STATE):
        server._MAIL_STATE.pop(t, None)
    for t in ("orch", "w1", "other"):
        server._QUEUE_STATE.pop(t, None)


def _strip(name: str) -> str:
    from backend.session import tmux

    for t in ("orch", "w1", "other"):
        if name == tmux.to_mindflock_tmux_name(t):
            return t
    return name


def _post(state, to, **body):
    return state.client.post("/api/instances/%s/messages" % to, json=body)


# --------------------------------------------------------------------------- #
# POST /messages — validation
# --------------------------------------------------------------------------- #
def test_post_to_an_unknown_session_is_404(env):
    _, st = env
    r = _post(st, "ghost", text="hi")
    assert r.status_code == 404


@pytest.mark.parametrize(
    "body, needle",
    [
        ({"text": "   "}, "empty"),
        ({"text": 5}, "string"),
        ({"text": "x" * 20001}, "too long"),
        ({"text": "x", "from": "nobody"}, "unknown sender"),
        ({"text": "x", "from": "w1"}, "itself"),
        ({"text": "x", "from": 3}, "from"),
        ({"text": "x", "delivery": "loud"}, "delivery"),
        ({"text": "x", "kind": "memo"}, "kind"),
        ({"text": "x", "data": [1]}, "object"),
        ({"text": "x", "data": {"k": "v" * 9000}}, "too large"),
        ({"text": "x", "reply_to": 7}, "reply_to"),
    ],
)
def test_post_validation(env, body, needle):
    _, st = env
    r = _post(st, "w1", **body)
    assert r.status_code == 400
    assert needle in r.json()["error"]
    assert mb.fetch("w1", unread_only=False)["messages"] == []


def test_exactly_20000_chars_is_allowed(env):
    _, st = env
    assert _post(st, "w1", text="x" * 20000, delivery="inbox").status_code == 201


# --------------------------------------------------------------------------- #
# POST /messages — delivery modes
# --------------------------------------------------------------------------- #
def test_auto_is_stored_pending_and_announced(env):
    _, st = env
    r = _post(st, "w1", text="please rebase", **{"from": "orch"})
    assert r.status_code == 201
    body = r.json()
    assert body["delivery"] == "pending" and "detail" not in body
    msg = body["message"]
    assert msg["state"] == "pending" and msg["from"] == "orch" and msg["to"] == "w1"
    assert st.typed == []  # auto never types from the route
    (ev,) = st.events
    assert ev["session"] == "w1"
    assert ev["data"] == {
        "id": msg["id"],
        "from": "orch",
        "kind": "message",
        "text": "please rebase",
        "delivery": "pending",
    }


def test_auto_to_a_stopped_agent_says_so(env):
    _, st = env
    st.live.discard("w1")
    body = _post(st, "w1", text="hi").json()
    assert body["delivery"] == "pending" and "isn't running" in body["detail"]


def test_inbox_is_held(env):
    _, st = env
    body = _post(st, "w1", text="fyi", delivery="inbox").json()
    assert body["delivery"] == "held" and body["message"]["state"] == "held"
    assert st.events[0]["data"]["delivery"] == "held"


def test_event_text_is_sanitized_and_capped(env):
    _, st = env
    _post(st, "w1", text="a\nb\x1b" + "c" * 400, delivery="inbox")
    text = st.events[0]["data"]["text"]
    assert text.startswith("a bc") and len(text) == 200


def test_now_from_outside_types_immediately_without_stamping_a_human(env):
    server, st = env
    body = _post(st, "w1", text="stop and commit", delivery="now").json()
    assert body["delivery"] == "delivered" and body["message"]["state"] == "delivered"
    ((name, line, submit),) = st.typed
    assert submit is True and "stop and commit" in line
    assert "from outside the flock" in line
    assert st.humans == []
    assert server._MAIL_STATE["w1"]["sent_at"] > 0


def test_now_from_an_ancestor_types(env):
    _, st = env
    body = _post(st, "w1", text="go", delivery="now", **{"from": "orch"}).json()
    assert body["delivery"] == "delivered"
    assert 'mcp__mindflock__send_message to="orch"' in st.typed[0][1]


def test_now_from_a_grandparent_types(env):
    _, st = env
    st.insts["orch"].Parent = "other"
    body = _post(st, "w1", text="go", delivery="now", **{"from": "other"}).json()
    assert body["delivery"] == "delivered"


def test_now_from_a_non_ancestor_falls_back_to_auto(env):
    _, st = env
    body = _post(st, "orch", text="go", delivery="now", **{"from": "w1"}).json()
    assert body["delivery"] == "pending"
    assert "only for your own descendants" in body["detail"]
    assert body["message"]["delivery"] == "auto"
    assert st.typed == []


@pytest.mark.parametrize("activity", ["clarify", "limit", "offline"])
def test_now_never_types_into_a_prompt_a_limit_menu_or_a_dead_agent(env, activity):
    _, st = env
    st.activity["w1"] = activity
    body = _post(st, "w1", text="1", delivery="now").json()
    assert (
        body["delivery"] == "pending" and "typed in once it is idle" in body["detail"]
    )
    assert st.typed == []
    assert mb.next_pending("w1")["id"] == body["message"]["id"]


def test_now_types_into_a_working_agent(env):
    _, st = env
    st.activity["w1"] = "working"
    assert _post(st, "w1", text="x", delivery="now").json()["delivery"] == "delivered"


def test_now_does_not_boot_a_stopped_agent(env):
    _, st = env
    st.live.discard("w1")
    body = _post(st, "w1", text="x", delivery="now").json()
    assert body["delivery"] == "pending" and "isn't running" in body["detail"]


def test_now_defers_to_an_inbox_long_poll(env):
    _, st = env
    mb.waiter_begin("w1")
    body = _post(st, "w1", text="x", delivery="now").json()
    assert body["delivery"] == "pending" and "inbox" in body["detail"]
    assert st.typed == []


def test_now_holds_for_a_paused_session(env):
    _, st = env
    st.insts["w1"].Status = Status.Paused
    body = _post(st, "w1", text="x", delivery="now").json()
    assert body["delivery"] == "pending" and "can't take input" in body["detail"]


def test_now_with_a_failed_send_stays_pending(env):
    _, st = env
    st.send_ok = False
    body = _post(st, "w1", text="x", delivery="now").json()
    assert body["delivery"] == "pending" and "retries" in body["detail"]
    assert body["message"]["state"] == "pending"


def test_now_with_a_long_body_types_a_notice_and_keeps_it_in_the_inbox(env):
    _, st = env
    body = _post(st, "w1", text="word " * 600, delivery="now").json()
    assert body["delivery"] == "delivered"
    assert "full text waits in the inbox" in body["detail"]
    assert body["message"]["state"] == "held"
    assert st.typed[0][1].endswith("(full text: call mcp__mindflock__check_inbox)")
    assert [m["id"] for m in mb.fetch("w1")["messages"]] == [body["message"]["id"]]


def test_the_seventh_push_in_ten_minutes_is_held_by_the_route(env):
    _, st = env
    for i in range(mb.RATE_PAIR_MAX):
        assert _post(st, "w1", text=str(i), **{"from": "orch"}).json()["delivery"] == (
            "pending"
        )
    body = _post(st, "w1", text="again", **{"from": "orch"}).json()
    assert body["delivery"] == "held" and body["detail"].startswith("rate limit")


def test_a_deep_reply_chain_is_held_by_the_route(env, monkeypatch):
    _, st = env
    monkeypatch.setenv("MINDFLOCK_MSG_MAX_HOPS", "1")
    a = _post(st, "w1", text="q", **{"from": "orch"}).json()["message"]
    b = _post(st, "orch", text="r", reply_to=a["id"], **{"from": "w1"}).json()
    assert b["message"]["hop"] == 1 and b["delivery"] == "pending"
    c = _post(st, "w1", text="rr", reply_to=b["message"]["id"], **{"from": "orch"})
    assert c.json()["delivery"] == "held"
    assert c.json()["detail"].startswith("reply chain limit")


def test_a_result_carries_its_data(env):
    _, st = env
    body = _post(
        st,
        "orch",
        text="all green",
        kind="result",
        data={"status": "done", "branch": "b"},
        **{"from": "w1"},
    ).json()
    assert body["message"]["kind"] == "result"
    assert body["message"]["data"] == {"status": "done", "branch": "b"}


def test_a_result_event_names_its_status(env):
    _, st = env
    _post(
        st,
        "orch",
        text="stuck on creds",
        kind="result",
        data={"status": "blocked"},
        **{"from": "w1"},
    )
    _post(st, "orch", text="plain note", **{"from": "w1"})
    result_ev, note_ev = st.events[-2:]
    assert result_ev["data"]["kind"] == "result"
    assert result_ev["data"]["status"] == "blocked"
    assert "status" not in note_ev["data"]


# --------------------------------------------------------------------------- #
# GET /messages + read
# --------------------------------------------------------------------------- #
def _get(st, title, **q):
    return st.client.get("/api/instances/%s/messages" % title, params=q)


def test_get_unknown_session_is_404(env):
    _, st = env
    assert _get(st, "ghost").status_code == 404


def test_get_lists_unread_and_marks_read_on_request(env):
    _, st = env
    ids = [_post(st, "w1", text=str(i)).json()["message"]["id"] for i in range(3)]
    out = _get(st, "w1").json()
    assert [m["id"] for m in out["messages"]] == ids and out["unread"] == 3
    out = _get(st, "w1", mark_read=1, limit=2).json()
    assert [m["state"] for m in out["messages"]] == ["read", "read"]
    assert out["unread"] == 1
    hist = _get(st, "w1", include_consumed=1).json()
    assert len(hist["messages"]) == 3
    assert _get(st, "w1", unread=0).json()["messages"] == hist["messages"]


def test_get_filters_by_sender_kind_and_after(env):
    _, st = env
    a = _post(st, "w1", text="a", **{"from": "orch"}).json()["message"]
    b = _post(st, "w1", text="b").json()["message"]
    c = _post(st, "w1", text="c", kind="result", **{"from": "other"}).json()["message"]
    assert [m["id"] for m in _get(st, "w1", **{"from": "orch"}).json()["messages"]] == [
        a["id"]
    ]
    assert [m["id"] for m in _get(st, "w1", **{"from": ""}).json()["messages"]] == [
        b["id"]
    ]
    assert [m["id"] for m in _get(st, "w1", kind="result").json()["messages"]] == [
        c["id"]
    ]
    assert [m["id"] for m in _get(st, "w1", after=b["id"]).json()["messages"]] == [
        c["id"]
    ]


@pytest.mark.parametrize(
    "q", [{"limit": "many"}, {"wait": "soon"}, {"kind": "memo"}, {"after": "x"}]
)
def test_get_rejects_bad_query(env, q):
    _, st = env
    assert _get(st, "w1", **q).status_code == 400


def test_get_clamps_limit(env):
    _, st = env
    for i in range(3):
        _post(st, "w1", text=str(i), delivery="inbox")
    assert len(_get(st, "w1", limit=0).json()["messages"]) == 1
    assert len(_get(st, "w1", limit=10_000).json()["messages"]) == 3


def test_long_poll_returns_as_soon_as_a_message_arrives(env):
    _, st = env
    seen_waiter = []

    def later():
        time.sleep(0.6)
        seen_waiter.append(mb.waiter_active("w1"))
        mb.post("w1", "here you go", sender="orch")

    t = threading.Thread(target=later)
    t.start()
    t0 = time.monotonic()
    out = _get(st, "w1", wait=10, mark_read=1).json()
    elapsed = time.monotonic() - t0
    t.join()
    assert [m["text"] for m in out["messages"]] == ["here you go"]
    assert out["messages"][0]["state"] == "read"
    assert 0.5 <= elapsed < 4.0
    assert seen_waiter == [True]
    # The poll ended, but the lane still holds through the tail.
    assert mb.waiter_active("w1") is True
    assert mb._WAITERS["w1"][0] == 0


def test_long_poll_ignores_a_change_that_does_not_match(env):
    _, st = env

    def later():
        time.sleep(0.3)
        mb.post("w1", "a message", sender="orch")
        time.sleep(0.5)
        mb.post("w1", "the result", sender="orch", kind="result")

    t = threading.Thread(target=later)
    t.start()
    out = _get(st, "w1", wait=10, kind="result").json()
    t.join()
    assert [m["text"] for m in out["messages"]] == ["the result"]


def test_long_poll_times_out_empty(env):
    _, st = env
    t0 = time.monotonic()
    out = _get(st, "w1", wait=1).json()
    elapsed = time.monotonic() - t0
    assert out["messages"] == [] and out["unread"] == 0
    assert 0.9 <= elapsed < 3.0


def test_long_poll_returns_immediately_when_something_is_waiting(env):
    _, st = env
    _post(st, "w1", text="already here", delivery="inbox")
    t0 = time.monotonic()
    out = _get(st, "w1", wait=10).json()
    assert len(out["messages"]) == 1 and time.monotonic() - t0 < 2.0


def test_long_poll_ends_when_the_recipient_goes_away(env):
    server, st = env

    def later():
        time.sleep(0.4)
        server.ENGINE.instances.pop("w1", None)

    t = threading.Thread(target=later)
    t.start()
    t0 = time.monotonic()
    out = _get(st, "w1", wait=10).json()
    t.join()
    assert out["messages"] == [] and time.monotonic() - t0 < 4.0


def test_get_without_wait_registers_no_waiter(env):
    _, st = env
    _get(st, "w1")
    assert "w1" not in mb._WAITERS


def test_read_route(env):
    _, st = env
    ids = [_post(st, "w1", text=str(i)).json()["message"]["id"] for i in range(3)]
    r = st.client.post("/api/instances/w1/messages/read", json={"ids": ids[:1]})
    assert r.json() == {"marked": 1, "unread": 2}
    r = st.client.post("/api/instances/w1/messages/read", json={"all": True})
    assert r.json() == {"marked": 2, "unread": 0}


@pytest.mark.parametrize("body", [{}, {"ids": "m1_1"}, {"ids": [1]}, {"all": "yes"}])
def test_read_route_rejects_bad_bodies(env, body):
    _, st = env
    r = st.client.post("/api/instances/w1/messages/read", json=body)
    assert r.status_code == 400


def test_read_route_unknown_session_is_404(env):
    _, st = env
    r = st.client.post("/api/instances/ghost/messages/read", json={"all": True})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Descendant rule
# --------------------------------------------------------------------------- #
def test_is_descendant_walks_the_live_chain_and_survives_cycles(env):
    server, st = env
    assert server._is_descendant("w1", "orch") is True
    assert server._is_descendant("orch", "w1") is False
    st.insts["orch"].Parent = "w1"  # a cycle
    assert server._is_descendant("w1", "other") is False
    st.insts["orch"].Parent = "gone"  # a link to a session that is not live
    assert server._is_descendant("w1", "gone") is True  # the direct link counts
    assert server._is_descendant("w1", "beyond") is False


def test_is_descendant_without_lineage_fields(env):
    server, _ = env
    assert server._is_descendant("other", "orch") is False
    assert server._is_descendant("ghost", "orch") is False


# --------------------------------------------------------------------------- #
# Delivery lane
# --------------------------------------------------------------------------- #
@pytest.fixture
def lane(env, monkeypatch):
    """The lane with a controllable clock."""
    server, st = env
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(server.time, "time", lambda: clock["now"])
    st.clock = clock
    return server, st


def _settle(server, st, title="w1", passes=1):
    """Run the lane until the idle dwell is satisfied, then once more."""
    server._drain_one_mailbox(title)  # first idle sighting starts the dwell
    st.clock["now"] += server._QUEUE_IDLE_SETTLE + 1
    for _ in range(passes):
        server._drain_one_mailbox(title)


def test_lane_types_after_the_idle_settles(lane):
    server, st = lane
    m = mb.post("w1", "hello", sender="orch", now=st.clock["now"])
    server._drain_one_mailbox("w1")
    assert st.typed == []  # the dwell has only started
    st.clock["now"] += server._QUEUE_IDLE_SETTLE - 1
    server._drain_one_mailbox("w1")
    assert st.typed == []
    st.clock["now"] += 2
    server._drain_one_mailbox("w1")
    ((name, line, submit),) = st.typed
    assert line.startswith('[MindFlock message %s from session "orch"' % m["id"])
    got = mb.get("w1", m["id"])
    assert got["state"] == "delivered" and got["delivered_ts"] == st.clock["now"]
    assert st.humans == []


def test_lane_takes_the_fast_settle_on_an_authoritative_idle(lane):
    from backend.web.core import agent_state

    server, st = lane
    agent_state._ACTIVITY_CACHE["w1"] = {
        "created": 1.0,
        "reported": "idle",
        "state_since": 0.0,
        "worked_at": None,
        "reading": ("idle", "marker"),
    }
    try:
        mb.post("w1", "hello", now=st.clock["now"])
        server._drain_one_mailbox("w1")
        st.clock["now"] += server._QUEUE_IDLE_SETTLE_MARKER + 1
        server._drain_one_mailbox("w1")
        assert len(st.typed) == 1
    finally:
        agent_state._ACTIVITY_CACHE.pop("w1", None)


@pytest.mark.parametrize("activity", ["working", "clarify", "limit", "offline"])
def test_lane_waits_out_every_non_idle_activity(lane, activity):
    server, st = lane
    mb.post("w1", "x")
    st.activity["w1"] = activity
    for _ in range(4):
        server._drain_one_mailbox("w1")
        st.clock["now"] += 30
    assert st.typed == []
    assert server._MAIL_STATE["w1"]["idle_since"] is None
    assert mb.next_pending("w1") is not None


def test_a_working_blip_restarts_the_dwell(lane):
    server, st = lane
    mb.post("w1", "x")
    server._drain_one_mailbox("w1")
    st.clock["now"] += server._QUEUE_IDLE_SETTLE - 1
    st.activity["w1"] = "working"
    server._drain_one_mailbox("w1")
    st.activity["w1"] = "idle"
    st.clock["now"] += 5
    server._drain_one_mailbox("w1")
    assert st.typed == []


def test_lane_never_boots_a_dead_agent(lane):
    server, st = lane
    mb.post("w1", "x")
    st.live.discard("w1")
    _settle(server, st)
    assert st.typed == [] and mb.next_pending("w1") is not None


def test_lane_holds_for_paused_and_budget_locked(lane, monkeypatch):
    server, st = lane
    mb.post("w1", "x")
    st.insts["w1"].Status = Status.Paused
    _settle(server, st)
    assert st.typed == []
    st.insts["w1"].Status = Status.Running
    monkeypatch.setattr(server, "_budget_locked", lambda t: True)
    _settle(server, st)
    assert st.typed == []
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    _settle(server, st)
    assert len(st.typed) == 1


def test_lane_holds_while_setup_runs(lane, monkeypatch):
    server, st = lane
    mb.post("w1", "x")
    monkeypatch.setattr(
        server._wt_setup, "setup_status", lambda wt: {"state": "running"}
    )
    _settle(server, st)
    assert st.typed == []


def test_lane_holds_while_a_long_poll_is_active(lane):
    server, st = lane
    mb.post("w1", "x")
    mb.waiter_begin("w1")
    _settle(server, st)
    assert st.typed == []
    mb.waiter_end("w1", now=st.clock["now"])
    st.clock["now"] += mb.WAITER_TAIL_S - 1
    server._drain_one_mailbox("w1")
    assert st.typed == []  # still inside the tail


def test_lane_holds_while_fast_track_runs(lane, monkeypatch):
    server, st = lane
    mb.post("w1", "x")
    monkeypatch.setattr(server, "_autopilot_running", lambda t: True)
    _settle(server, st)
    assert st.typed == []


def test_lane_holds_inside_the_queue_reboot_grace(lane):
    server, st = lane
    mb.post("w1", "x")
    _settle(server, st, passes=0)
    server._QUEUE_STATE["w1"] = {
        "armed": True,
        "sent_at": 0.0,
        "rebooted_at": st.clock["now"] - 1,
        "idle_since": None,
    }
    server._drain_one_mailbox("w1")
    assert st.typed == []


def test_lane_holds_under_a_usage_limit_banner(lane, monkeypatch):
    server, st = lane
    mb.post("w1", "x")
    monkeypatch.setattr(
        server, "_refresh_limit_state", lambda i, t, n: st.clock["now"] + 600
    )
    _settle(server, st)
    assert st.typed == []


def test_lane_types_one_message_per_pass_and_resettles(lane):
    server, st = lane
    a = mb.post("w1", "first")
    b = mb.post("w1", "second")
    _settle(server, st)
    assert len(st.typed) == 1 and "first" in st.typed[0][1]
    assert mb.get("w1", a["id"])["state"] == "delivered"
    st.clock["now"] += 1
    server._drain_one_mailbox("w1")
    assert len(st.typed) == 1  # the dwell restarted with the send
    st.clock["now"] += server._QUEUE_IDLE_SETTLE + 1
    server._drain_one_mailbox("w1")
    assert len(st.typed) == 2 and "second" in st.typed[1][1]
    assert mb.get("w1", b["id"])["state"] == "delivered"


def test_lane_respects_the_queue_send_cooldown(lane):
    server, st = lane
    mb.post("w1", "x")
    server._drain_one_mailbox("w1")
    st.clock["now"] += server._QUEUE_IDLE_SETTLE + 1
    server._QUEUE_STATE["w1"] = {
        "armed": False,
        "sent_at": st.clock["now"] - 1,
        "rebooted_at": 0.0,
        "idle_since": None,
    }
    server._drain_one_mailbox("w1")
    assert st.typed == []  # the queue just typed: the dwell restarts
    st.clock["now"] += server._QUEUE_IDLE_SETTLE + 1
    server._drain_one_mailbox("w1")
    assert len(st.typed) == 1


def test_a_ready_user_prompt_goes_first(lane):
    server, st = lane
    mb.post("w1", "x")
    pq.enqueue("w1", "user prompt")
    _settle(server, st)
    assert st.typed == []
    pq.set_flags("w1", enabled=False)  # a paused queue starves nobody
    server._drain_one_mailbox("w1")
    assert len(st.typed) == 1


def test_a_timed_loop_between_rounds_does_not_starve_messages(lane):
    server, st = lane
    pq.enqueue("w1", "improve")
    pq.set_flags("w1", loop=True, loop_interval=30)
    pq.record_sent("w1", pq.list_queue("w1")[0]["id"])  # requeued, last_sent=now
    assert server._queue_wants_turn("w1", time.time()) is False


def test_a_disarmed_queue_does_not_hold_messages(lane):
    server, st = lane
    pq.enqueue("w1", "p")
    server._QUEUE_STATE["w1"] = {
        "armed": False,
        "sent_at": 0.0,
        "rebooted_at": 0.0,
        "idle_since": None,
    }
    assert server._queue_wants_turn("w1", st.clock["now"]) is False


def test_lane_never_touches_the_users_queue(lane):
    server, st = lane
    pq.enqueue("w1", "mine")
    pq.set_flags("w1", enabled=False, loop=True, loop_interval=7)
    before = pq.get_state("w1")
    mb.post("w1", "x")
    _settle(server, st)
    assert len(st.typed) == 1
    assert pq.get_state("w1") == before


def test_read_before_the_lane_means_nothing_is_typed(lane):
    server, st = lane
    mb.post("w1", "x")
    server._drain_one_mailbox("w1")
    mb.fetch("w1", mark_read=True)
    st.clock["now"] += server._QUEUE_IDLE_SETTLE + 1
    server._drain_one_mailbox("w1")
    assert st.typed == []


def test_a_fetch_racing_the_claim_wins_cleanly(lane, monkeypatch):
    server, st = lane
    m = mb.post("w1", "x")
    real_claim = mb.claim

    def fetch_first(*a, **k):
        mb.fetch("w1", mark_read=True)
        return real_claim(*a, **k)

    monkeypatch.setattr(server._mailbox, "claim", fetch_first)
    _settle(server, st)
    assert st.typed == []
    assert mb.get("w1", m["id"])["state"] == "read"


def test_a_failed_send_is_retried_later(lane):
    server, st = lane
    m = mb.post("w1", "x")
    st.send_ok = False
    _settle(server, st)
    assert mb.get("w1", m["id"])["state"] == "pending"
    st.send_ok = True
    st.clock["now"] += 5
    server._drain_one_mailbox("w1")
    assert len(st.typed) == 1
    assert mb.get("w1", m["id"])["state"] == "delivered"


def test_lane_types_a_notice_for_a_long_body(lane):
    server, st = lane
    m = mb.post("w1", "y" * 5000, sender="orch")
    _settle(server, st)
    assert st.typed[0][1].endswith("(full text: call mcp__mindflock__check_inbox)")
    got = mb.get("w1", m["id"])
    assert got["state"] == "held" and got["delivered_ts"] is not None
    assert mb.next_pending("w1") is None  # never typed twice
    assert [x["id"] for x in mb.fetch("w1")["messages"]] == [m["id"]]


def test_lane_uses_the_recipients_provider_for_the_hint(lane):
    server, st = lane
    mb.post("other", "x", sender="orch")
    _settle(server, st, title="other")
    assert 'the send_message tool of the "mindflock" MCP server' in st.typed[0][1]


def test_queue_drain_respects_a_mailbox_send(lane, monkeypatch):
    server, st = lane
    # The USER's queue may (re)attach its agent; only the mailbox may not.
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda i, t: ("agent_" + t, None)
    )
    pq.enqueue("w1", "user prompt")
    server._QUEUE_STATE["w1"] = {
        "armed": True,
        "sent_at": 0.0,
        "rebooted_at": 0.0,
        "idle_since": 1.0,
    }
    server._MAIL_STATE["w1"] = {"idle_since": None, "sent_at": st.clock["now"] - 1}
    server._drain_one_queue("w1")
    assert st.typed == []  # cooldown across both typers, and the dwell restarts
    st.clock["now"] += server._QUEUE_IDLE_SETTLE + 1
    server._drain_one_queue("w1")
    assert [t for _, t, _ in st.typed] == ["user prompt"]


def test_a_full_pass_sends_the_user_prompt_before_the_message(lane, monkeypatch):
    server, st = lane
    # The USER's queue may (re)attach its agent; only the mailbox may not.
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda i, t: ("agent_" + t, None)
    )
    pq.enqueue("w1", "user prompt")
    mb.post("w1", "agent message")
    server._QUEUE_STATE["w1"] = {
        "armed": True,
        "sent_at": 0.0,
        "rebooted_at": 0.0,
        "idle_since": 1.0,
    }
    server._MAIL_STATE["w1"] = {"idle_since": 1.0, "sent_at": 0.0}
    server._drain_prompt_queues()
    assert [t for _, t, _ in st.typed] == ["user prompt"]
    assert mb.next_pending("w1") is not None


def test_drain_mailboxes_prunes_old_dead_boxes_and_forgets_state(lane):
    server, st = lane
    mb.post("gone", "x", now=st.clock["now"] - server._MAIL_PRUNE_AFTER_S - 5)
    mb.post("young-gone", "x", now=st.clock["now"])
    server._MAIL_STATE["gone"] = {"idle_since": None, "sent_at": 0.0}
    server._drain_mailboxes()
    assert mb.version("gone") == 0
    assert mb.version("young-gone") > 0
    assert "gone" not in server._MAIL_STATE


def test_drain_mailboxes_survives_a_broken_session(lane, monkeypatch):
    server, st = lane
    mb.post("orch", "x")
    mb.post("w1", "y")
    calls = []

    def flaky(title):
        calls.append(title)
        if title == "orch":
            raise RuntimeError("boom")

    monkeypatch.setattr(server, "_drain_one_mailbox", flaky)
    server._drain_mailboxes()
    assert sorted(calls) == ["orch", "w1"]


def test_autopilot_running_needs_a_live_lease(env, monkeypatch):
    server, _ = env
    now = time.time()
    recs = {
        "fresh": {"state": "running", "owner_at": now},
        "stale": {"state": "running", "owner_at": now - 10_000},
        "done": {"state": "done", "owner_at": now},
        "garbage": {"state": "running", "owner_at": "soon"},
    }
    monkeypatch.setattr(server._autopilot, "get", lambda t: recs.get(t))
    assert server._autopilot_running("fresh") is True
    assert server._autopilot_running("stale") is False
    assert server._autopilot_running("done") is False
    assert server._autopilot_running("none") is False
    assert server._autopilot_running("garbage") is True  # unreadable: hold


# --------------------------------------------------------------------------- #
# Review fixes: never type into a bare shell, over a human, or into a booting
# CLI                                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("shell", ["bash", "zsh", "sh"])
def test_lane_never_types_into_a_bare_shell(lane, shell):
    """A provisioned launcher drops to ``bash -i`` after the user quits the
    agent; that pane still reads idle. Typing there ran the body as shell."""
    server, st = lane
    st.fg["w1"] = shell
    m = mb.post("w1", "run `touch PWNED` now", sender="orch", now=st.clock["now"])
    _settle(server, st, passes=3)
    assert st.typed == []
    assert mb.get("w1", m["id"])["state"] == "pending"
    # A bare-shell wrapper with the agent alive underneath is fine.
    st.agent_child = True
    _settle(server, st)
    assert len(st.typed) == 1


@pytest.mark.parametrize(
    "fg,proc",
    [
        ("vim", "vim notes.md"),
        ("ssh", "ssh prod"),
        ("python3", "python3"),
        ("psql", "psql app"),
        ("ssh", "ssh claude"),  # an ARGUMENT naming an agent is not the agent
    ],
)
def test_lane_never_types_into_a_program_started_after_the_agent_quit(lane, fg, proc):
    """After a deliberate quit the launcher's ``bash -i`` runs whatever the
    user starts: any non-shell foreground used to read as the agent, so a
    sibling's message (backticks and all) was typed with Enter into vim, a
    remote shell over ssh, a REPL. Delivery needs the AGENT in the pane."""
    server, st = lane
    st.fg["w1"] = fg
    st.procs["w1"] = ["bash -i", proc]
    m = mb.post("w1", "run `touch PWNED` now", sender="orch", now=st.clock["now"])
    _settle(server, st, passes=3)
    assert st.typed == []
    assert mb.get("w1", m["id"])["state"] == "pending"
    r = _post(st, "w1", text="x", **{"from": "orch"}, delivery="now")
    assert r.json()["delivery"] == "pending" and st.typed == []


@pytest.mark.parametrize(
    "fg,procs",
    [
        ("claude", []),
        ("node", ["node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]),
        ("node", ["node /home/u/.npm-global/bin/claude --continue"]),
        ("bash", ["bash /home/u/ws/launch.sh", "claude --resume x"]),
    ],
)
def test_lane_recognises_the_agent_however_it_was_launched(lane, fg, procs):
    server, st = lane
    st.fg["w1"] = fg
    st.procs["w1"] = procs
    mb.post("w1", "hello", sender="orch", now=st.clock["now"])
    _settle(server, st)
    assert len(st.typed) == 1


def test_now_never_types_into_a_bare_shell(env):
    _, st = env
    st.fg["w1"] = "bash"
    r = _post(st, "w1", text="x; touch PWNED #", **{"from": "orch"}, delivery="now")
    assert r.status_code == 201
    assert r.json()["delivery"] == "pending"
    assert st.typed == []


def test_lane_holds_while_a_human_is_composing(lane):
    server, st = lane
    m = mb.post("w1", "please rebase", sender="orch", now=st.clock["now"])
    server._HUMAN_INPUT_AT["w1"] = st.clock["now"] - 1
    _settle(server, st, passes=2)
    assert st.typed == []
    # Once the stamp is older than the hold, the lane settles and types.
    st.clock["now"] += server._HUMAN_HOLD_S
    _settle(server, st)
    assert len(st.typed) == 1 and mb.get("w1", m["id"])["state"] == "delivered"


def test_lane_holds_for_raw_tmux_client_input(lane):
    server, st = lane
    mb.post("w1", "please rebase", sender="orch", now=st.clock["now"])
    st.tmux_typing.add("w1")
    _settle(server, st, passes=2)
    assert st.typed == []
    st.tmux_typing.clear()
    _settle(server, st)
    assert len(st.typed) == 1


def test_now_downgrades_to_pending_while_a_human_is_composing(env):
    server, st = env
    server._HUMAN_INPUT_AT["w1"] = server.time.time()
    r = _post(st, "w1", text="x", **{"from": "orch"}, delivery="now")
    assert r.json()["delivery"] == "pending"
    assert "typing" in r.json()["detail"]
    assert st.typed == []


def test_now_holds_inside_the_queue_boot_grace(env):
    """The queue just relaunched the CLI: typed text would be lost while the
    store said delivered."""
    server, st = env
    server._QUEUE_STATE["w1"] = {"rebooted_at": server.time.time()}
    r = _post(st, "w1", text="x", **{"from": "orch"}, delivery="now")
    assert r.json()["delivery"] == "pending"
    assert "starting" in r.json()["detail"]
    assert st.typed == []
    (m,) = mb.fetch("w1")["messages"]
    assert m["state"] == "pending"


def test_a_result_carries_a_fresh_diff_stat(env, monkeypatch):
    """The row's diff_stat is the tick's (cached ~10-14s): a worker that
    commits and reports at once used to send all zeros."""
    server, st = env
    calls = []
    stale = {"files": 0, "additions": 0, "deletions": 0}
    fresh = {"files": 1, "additions": 2, "deletions": 0}

    def _stat(inst):
        calls.append(inst.Title)
        return fresh

    monkeypatch.setattr(server, "_session_diff_stat", _stat)
    r = _post(
        st,
        "orch",
        text="done",
        **{"from": "w1"},
        kind="result",
        data={"status": "done", "diff_stat": stale},
    )
    assert r.status_code == 201
    assert r.json()["message"]["data"]["diff_stat"] == fresh
    assert r.json()["message"]["data"]["status"] == "done"
    assert calls == ["w1"]
    # A plain message is left alone.
    r = _post(st, "orch", text="hi", **{"from": "w1"}, data={"diff_stat": stale})
    assert r.json()["message"]["data"]["diff_stat"] == stale
