"""The prompt queue waits on an orchestrator's workers (docs/queue-waits-on-workers.md).

An orchestrator's turn ends while it waits — it was told it may end its turn
and have reports typed in, and Claude Code backgrounds a long MCP wait after
~2 min — so its pane reads idle with workers still running. The drain used to
type the next queued prompt into the middle of the orchestration. Now the
queue holds while a child is unfinished, lets the workers' reports go in
first, and has escapes so a stuck child cannot hold it forever.

Hermetic: the engine's instances are replaced by a dict of stand-ins, every
tmux/probe collaborator the drain and the mailbox lane reach is faked, and the
child's activity is the memo the state tick keeps (``agent_state._ACTIVITY_CACHE``).
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from types import SimpleNamespace

import pytest

from backend.session.storage import GitWorktreeData, InstanceData, Status
from backend.web.core import agent_state
from backend.web.core import mailbox as mb
from backend.web.core import prompt_queue as pq
from backend.web.core import queue_driver as qd

T0 = 1_000_000.0


def _mk_inst(title, wt, *, created, program="claude"):
    from backend.session.instance import FromInstanceData

    t = _dt.datetime.fromtimestamp(created, _dt.timezone.utc)
    data = InstanceData(
        title=title,
        path=wt,
        branch="b",
        status=Status.Running,
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
def fl(tmp_path, monkeypatch):
    """``orch`` (parent of ``w1`` and ``w2``) and a childless ``solo``, on a
    replaced engine, with a controllable clock."""
    from backend.web import server
    from backend.session import tmux

    monkeypatch.setenv("MINDFLOCK_PROMPT_QUEUE_FILE", str(tmp_path / "pq.json"))
    monkeypatch.setattr(server.ENGINE, "instances", {})
    clock = {"now": T0}
    monkeypatch.setattr(server.time, "time", lambda: clock["now"])
    insts = {}
    for title in ("orch", "w1", "w2", "solo"):
        inst = _mk_inst(title, str(tmp_path / title), created=T0 - 120)
        server.ENGINE.instances[title] = inst
        insts[title] = inst
    insts["w1"].Parent = "orch"
    insts["w2"].Parent = "orch"

    st = SimpleNamespace(
        typed=[],
        activity={"orch": "idle", "solo": "idle", "w1": "idle", "w2": "idle"},
        events=[],
        clock=clock,
        insts=insts,
    )

    def strip(name):
        for t in st.activity:
            if name == tmux.to_mindflock_tmux_name(t) or name == "agent_" + t:
                return t
        return name

    def fake_send(name, text, submit=True):
        st.typed.append((strip(name), text))
        return True

    monkeypatch.setattr(server, "_send_to_agent", fake_send)
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda i, t: ("agent_" + t, None)
    )
    monkeypatch.setattr(server, "_live_session_name", lambda n: n)
    monkeypatch.setattr(
        server, "_agent_activity", lambda inst, title: st.activity[title]
    )
    monkeypatch.setattr(server, "_refresh_limit_state", lambda i, t, n: 0.0)
    monkeypatch.setattr(
        server, "_pane_meta", lambda n: ("claude", 1.0, "4242", "80x24")
    )
    monkeypatch.setattr(
        server._agent_state, "_pane_runs_agent", lambda pid, names: True
    )
    monkeypatch.setattr(server, "_tmux_client_input_recent", lambda t, w: False)
    monkeypatch.setattr(server, "_HUMAN_INPUT_AT", {})
    monkeypatch.setattr(server, "_dialog_on_screen", lambda inst, name: False)
    from backend.web.core import queue_driver as qd

    monkeypatch.setattr(qd, "_dialog_on_screen", lambda inst, name: False)
    monkeypatch.setattr(qd, "_took_off_on_prompt", lambda title: None)

    def wipe():
        for t in st.activity:
            server._QUEUE_STATE.pop(t, None)
            server._MAIL_STATE.pop(t, None)
            qd._WORKER_HOLD.pop(t, None)
            agent_state._ACTIVITY_CACHE.pop(t, None)
        mb._WAITERS.clear()

    wipe()
    unsub = server._events.BUS.subscribe(
        lambda e: (
            st.events.append(e)
            if e["event"] == "session.queue_changed" and e["session"] in st.activity
            else None
        )
    )
    yield server, st
    unsub()
    wipe()


def _child(title, reading, since=T0 - 60):
    """The state tick's memo for a child: its last reading and since when."""
    agent_state._ACTIVITY_CACHE[title] = {
        "created": 1.0,
        "reported": reading,
        "state_since": since,
        "worked_at": None,
        "reading": (reading, "marker"),
    }


def _passes(server, st, title, n=4, step=5.0):
    """``n`` drain passes ``step`` seconds apart (the real cadence)."""
    for _ in range(n):
        server._drain_one_queue(title)
        st.clock["now"] += step


def _queue_typed(st, title):
    return [
        t for (who, t) in st.typed if who == title and not t.startswith("[MindFlock")
    ]


def _report(sender, status="done", to="orch", now=None):
    return mb.post(
        to,
        "%s finished" % sender,
        sender=sender,
        kind="result",
        data={"status": status},
        now=now,
    )


# --------------------------------------------------------------------------- #
# The hold
# --------------------------------------------------------------------------- #
def test_an_idle_orchestrator_with_a_running_child_is_not_typed_into(fl):
    server, st = fl
    _child("w1", "working")
    _child("w2", "working")
    pq.enqueue("orch", "next task")
    _passes(server, st, "orch", n=12)  # a minute of idle, settled many times over
    assert _queue_typed(st, "orch") == []
    assert [i["text"] for i in pq.list_queue("orch")] == ["next task"]
    # The hold is announced on the queue's own event, naming who it waits on.
    (ev,) = [e for e in st.events if e["session"] == "orch"]
    assert ev["data"]["waiting_on"] == ["w1", "w2"]
    assert "waiting on 2 workers" in ev["data"]["waiting"]
    from backend.web.core import queue_driver as qd

    hold = qd.queue_hold("orch")
    assert hold["workers"] == {"w1": "working", "w2": "working"}


def test_reports_are_typed_first_then_the_queue_sends(fl):
    server, st = fl
    _child("w1", "working")
    _child("w2", "working")
    pq.enqueue("orch", "next task")
    _passes(server, st, "orch", n=3)
    # Both workers finish and report; their reports wait in orch's mailbox.
    _child("w1", "idle", since=st.clock["now"])
    _child("w2", "idle", since=st.clock["now"])
    r1 = _report("w1", now=st.clock["now"])
    r2 = _report("w2", status="blocked", now=st.clock["now"])
    st.clock["now"] += 1
    # The queue still holds: reports pending (and the reaction grace).
    _passes(server, st, "orch", n=2)
    assert _queue_typed(st, "orch") == []
    # The mailbox lane, which the queue no longer pre-empts, types them.
    for _ in range(8):
        server._drain_one_mailbox("orch")
        st.clock["now"] += 5
    lines = [t for (who, t) in st.typed if who == "orch"]
    assert len(lines) == 2
    assert r1["id"] in lines[0] and r2["id"] in lines[1]
    assert _queue_typed(st, "orch") == []
    # The orchestrator takes its turn on them, then ends it.
    st.activity["orch"] = "working"
    _passes(server, st, "orch", n=2)
    st.activity["orch"] = "idle"
    st.clock["now"] += qd._QUEUE_WORKER_GRACE
    _passes(server, st, "orch", n=6)
    assert _queue_typed(st, "orch") == ["next task"]
    # And the hold's end was announced.
    assert st.events[-1]["data"]["waiting_on"] == []


def test_the_queue_yields_to_pending_reports(fl):
    """``_queue_wants_turn`` (the mailbox lane's 'user prompt first' gate)
    answers False while the queue holds for the workers."""
    server, st = fl
    _child("w1", "idle")
    _child("w2", "idle")
    _report("w1", now=st.clock["now"] - 120)
    _report("w2", now=st.clock["now"] - 120)
    pq.enqueue("orch", "next task")
    assert server._queue_wants_turn("orch", st.clock["now"]) is False
    for m in mb.fetch("orch", mark_read=True)["messages"]:
        assert m["kind"] == "result"
    assert server._queue_wants_turn("orch", st.clock["now"]) is True


def test_a_stuck_child_cannot_hold_the_queue_forever(fl):
    """An unreported child idle past the escape stops holding."""
    server, st = fl
    _child("w1", "idle", since=st.clock["now"])
    _child("w2", "offline")  # closed / stopped: finished
    pq.enqueue("orch", "next task")
    _passes(server, st, "orch", n=6)
    assert _queue_typed(st, "orch") == []
    st.clock["now"] += qd._QUEUE_WORKER_IDLE_ESCAPE
    _passes(server, st, "orch", n=6)
    assert _queue_typed(st, "orch") == ["next task"]


def test_a_child_working_forever_is_capped(fl):
    server, st = fl
    _child("w1", "working")
    _child("w2", "offline")
    pq.enqueue("orch", "next task")
    _passes(server, st, "orch", n=4)
    assert _queue_typed(st, "orch") == []
    st.clock["now"] += qd._QUEUE_WORKER_HOLD_MAX
    _passes(server, st, "orch", n=6)
    assert _queue_typed(st, "orch") == ["next task"]


@pytest.mark.parametrize("reading", ["clarify", "limit", "offline"])
def test_a_child_on_a_dialog_limit_or_offline_does_not_hold(fl, reading):
    server, st = fl
    _child("w1", reading)
    _child("w2", "offline")
    pq.enqueue("orch", "next task")
    _passes(server, st, "orch", n=6)
    assert _queue_typed(st, "orch") == ["next task"]


def test_a_session_with_no_children_is_unchanged(fl):
    server, st = fl
    pq.enqueue("solo", "task one")
    server._drain_one_queue("solo")  # first idle sighting starts the dwell
    assert st.typed == []
    st.clock["now"] += server._QUEUE_IDLE_SETTLE + 1
    server._drain_one_queue("solo")
    assert _queue_typed(st, "solo") == ["task one"]
    assert [e for e in st.events if e["session"] == "solo"] == []


def test_a_child_held_by_its_order_holds_only_while_a_sibling_runs(fl):
    server, st = fl
    _child("w1", "working")
    _child("w2", "idle")
    pq.set_flags("w2", enabled=False, held=True)
    pq.enqueue("w2", "w2's task")
    pq.enqueue("orch", "next task")
    from backend.web.core import queue_driver as qd

    assert qd.queue_hold("orch", st.clock["now"])["workers"] == {
        "w1": "working",
        "w2": "queued",
    }
    # w1 stopped (blocked): w2 waits on a person, not on time — no hold.
    _child("w1", "idle")
    _report("w1", status="blocked", now=st.clock["now"] - 120)
    mb.fetch("orch", mark_read=True)
    assert qd.queue_hold("orch", st.clock["now"]) is None


def test_a_report_older_than_a_follow_up_does_not_count(fl):
    """The orchestrator sent its worker more work after its report: the
    worker is out again until it reports again."""
    from backend.web.core import queue_driver as qd

    server, st = fl
    _child("w2", "offline")
    _report("w1", now=st.clock["now"] - 60)
    mb.fetch("orch", mark_read=True)
    _child("w1", "working")
    assert qd.queue_hold("orch", st.clock["now"]) is None  # reported: finished
    mb.post("w1", "one more thing", sender="orch", now=st.clock["now"] - 40)
    assert qd.queue_hold("orch", st.clock["now"])["workers"] == {"w1": "working"}


def test_a_child_whose_task_is_still_queued_holds(fl):
    """A just-spawned worker's task waits in its own queue (its CLI is still
    booting, its setup still running): that is 'starting', not 'stuck'."""
    from backend.web.core import queue_driver as qd

    server, st = fl
    _child("w2", "offline")
    _child("w1", "idle", since=st.clock["now"] - 10 * qd._QUEUE_WORKER_IDLE_ESCAPE)
    pq.enqueue("w1", "the task")
    assert qd.queue_hold("orch", st.clock["now"])["workers"] == {"w1": "starting"}


def test_an_inbox_long_poll_holds_the_queue(fl):
    from backend.web.core import queue_driver as qd

    server, st = fl
    pq.enqueue("solo", "task one")
    mb.waiter_begin("solo")
    _passes(server, st, "solo", n=6)
    assert st.typed == []
    assert qd.queue_hold("solo", st.clock["now"])["inbox_wait"] is True
    mb.waiter_end("solo", now=st.clock["now"])
    st.clock["now"] += mb.WAITER_TAIL_S + 1
    _passes(server, st, "solo", n=6)
    assert _queue_typed(st, "solo") == ["task one"]


def test_the_clock_fixture_drives_queue_holds_default_now(fl):
    """``queue_hold`` defaults ``now`` to ``time.time()``: the fixture's clock
    must reach the queue driver too, not only ``server``."""
    server, st = fl
    _child("w1", "working")
    _child("w2", "offline")
    assert qd.time.time() == st.clock["now"]
    assert qd.queue_hold("orch")["since"] == st.clock["now"]


# --------------------------------------------------------------------------- #
# Who holds, who does not
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reading", ["working", "idle", None])
def test_a_report_after_the_last_dispatch_wins_over_the_reading(fl, reading):
    """The child reported since the orchestrator last messaged it: finished,
    whatever its pane reads (a worker tidying up after its report)."""
    server, st = fl
    _child("w2", "offline")
    if reading is None:
        agent_state._ACTIVITY_CACHE.pop("w1", None)
    else:
        _child("w1", reading, since=st.clock["now"])
    mb.post("w1", "do the thing", sender="orch", now=st.clock["now"] - 200)
    _report("w1", now=st.clock["now"] - 120)  # past the reaction grace
    mb.fetch("orch", mark_read=True)
    assert qd.queue_hold("orch", st.clock["now"]) is None


def test_a_paused_child_does_not_hold(fl):
    server, st = fl
    _child("w1", "working")
    _child("w2", "offline")
    st.insts["w1"].Status = Status.Paused
    assert qd.queue_hold("orch", st.clock["now"]) is None


def test_a_usage_limited_child_does_not_hold(fl, monkeypatch):
    server, st = fl
    _child("w1", "working")
    _child("w2", "offline")
    now = st.clock["now"]
    monkeypatch.setattr(
        server, "_session_limited_until", lambda t: now + 600 if t == "w1" else 0.0
    )
    assert qd.queue_hold("orch", now) is None
    monkeypatch.setattr(server, "_session_limited_until", lambda t: now - 1)
    assert qd.queue_hold("orch", now)["workers"] == {"w1": "working"}


def test_an_unstarted_child_is_starting_until_the_escape_from_created(fl):
    """Not started (or no reading at all): 'starting', and the escape counts
    from the child's CreatedAt — a fresh ``state_since`` does not extend it."""
    server, st = fl
    _child("w2", "offline")
    st.insts["w1"]._started = False
    _child("w1", "idle", since=st.clock["now"])
    created = T0 - 120  # the fixture's CreatedAt
    assert qd.queue_hold("orch", st.clock["now"])["workers"] == {"w1": "starting"}
    late = created + qd._QUEUE_WORKER_IDLE_ESCAPE + 1
    _child("w1", "idle", since=late - 1)  # its reading only just changed
    assert qd.queue_hold("orch", late) is None


def test_a_child_with_no_reading_is_starting_until_the_escape(fl):
    server, st = fl
    _child("w2", "offline")
    agent_state._ACTIVITY_CACHE.pop("w1", None)
    created = T0 - 120
    assert qd.queue_hold("orch", st.clock["now"])["workers"] == {"w1": "starting"}
    assert qd.queue_hold("orch", created + qd._QUEUE_WORKER_IDLE_ESCAPE + 1) is None


def test_a_started_idle_child_escapes_from_its_state_since(fl):
    """Started, unreported and idle: holds for the escape measured from when
    it went idle (later than its CreatedAt)."""
    server, st = fl
    _child("w2", "offline")
    went_idle = st.clock["now"] - 10
    _child("w1", "idle", since=went_idle)
    assert qd.queue_hold("orch", st.clock["now"])["workers"] == {"w1": "idle"}
    assert qd.queue_hold("orch", went_idle + qd._QUEUE_WORKER_IDLE_ESCAPE - 1)
    assert qd.queue_hold("orch", went_idle + qd._QUEUE_WORKER_IDLE_ESCAPE) is None


def test_a_grandchild_does_not_hold_the_grandparent(fl):
    """Only direct children count: a worker's own sub-worker still running
    under a finished worker leaves the orchestrator's queue free."""
    server, st = fl
    g1 = _mk_inst("g1", str(st.insts["w1"].Path) + "-g1", created=T0 - 120)
    g1.Parent = "w1"
    server.ENGINE.instances["g1"] = g1
    st.activity["g1"] = "working"  # the fixture's wipe covers it too
    _child("g1", "working")
    _child("w1", "offline")
    _child("w2", "offline")
    assert qd.queue_hold("orch", st.clock["now"]) is None
    assert qd.queue_hold("w1", st.clock["now"])["workers"] == {"g1": "working"}


def test_a_result_from_a_non_child_still_counts_as_worker_mail(fl):
    """Pinned: any pending ``result`` holds (reports go to a parent by
    construction), not only one from a current child."""
    server, st = fl
    _child("w1", "offline")
    _child("w2", "offline")
    mb.post("orch", "solo finished", sender="solo", kind="result", now=T0 - 120)
    hold = qd.queue_hold("orch", st.clock["now"])
    assert hold["workers"] == {} and hold["mail"] == 1
    # A plain message from a non-child does not.
    mb.fetch("orch", mark_read=True)
    mb.post("orch", "hello", sender="solo", now=T0 - 60)
    assert qd.queue_hold("orch", st.clock["now"]) is None


# --------------------------------------------------------------------------- #
# Fail open
# --------------------------------------------------------------------------- #
def test_mailbox_fetch_errors_are_swallowed(fl, monkeypatch):
    """An unreadable mailbox: the later ask is unknown (the report counts)
    and no worker mail is seen — and the drain does not raise."""
    server, st = fl
    _child("w1", "working")
    _child("w2", "offline")
    _report("w1", now=st.clock["now"] - 120)
    mb.fetch("orch", mark_read=True)
    mb.post("w1", "one more thing", sender="orch", now=st.clock["now"] - 60)
    assert qd.queue_hold("orch", st.clock["now"])["workers"] == {"w1": "working"}

    def boom(*a, **k):
        raise OSError("mailbox unreadable")

    monkeypatch.setattr(mb, "fetch", boom)
    assert qd._last_dispatch("orch", "w1") == 0.0
    assert qd._pending_from_workers("orch", ["w1", "w2"]) == 0
    assert qd.queue_hold("orch", st.clock["now"]) is None
    pq.enqueue("orch", "next task")
    _passes(server, st, "orch", n=6)
    assert _queue_typed(st, "orch") == ["next task"]


def test_a_broken_hold_read_fails_open(fl, monkeypatch):
    server, st = fl

    def boom(title, now):
        raise RuntimeError("bad read")

    monkeypatch.setattr(qd, "_hold_reason", boom)
    assert qd.queue_hold("orch", st.clock["now"]) is None
    pq.enqueue("orch", "next task")
    _passes(server, st, "orch", n=6)
    assert _queue_typed(st, "orch") == ["next task"]


# --------------------------------------------------------------------------- #
# The episode and its cap
# --------------------------------------------------------------------------- #
def test_queue_hold_alone_never_starts_an_episode(fl):
    """Pinned: without a drain-tracked episode ``since`` is ``now`` and the
    3 h cap never applies — only ``_track_hold`` (the drain) starts the clock.
    The drain reaches it on every idle pass, which is also the only time the
    mailbox lane could type, so ``_queue_wants_turn`` yields only while the
    drain has not yet capped the same hold."""
    server, st = fl
    _child("w1", "working", since=T0 - 10 * qd._QUEUE_WORKER_HOLD_MAX)
    _child("w2", "offline")
    late = T0 + 2 * qd._QUEUE_WORKER_HOLD_MAX
    hold = qd.queue_hold("orch", late)
    assert hold["since"] == late
    assert "orch" not in qd._WORKER_HOLD
    # Once the drain tracks the hold, the cap applies to queue_hold as well.
    qd._track_hold("orch", T0)
    assert qd.queue_hold("orch", T0 + qd._QUEUE_WORKER_HOLD_MAX) is None
    assert qd.queue_hold("orch", T0 + qd._QUEUE_WORKER_HOLD_MAX - 1)["since"] == T0


def test_drain_prunes_holds_of_vanished_sessions(fl, monkeypatch):
    """A reused title must not inherit an old episode's ``since``."""
    server, st = fl
    monkeypatch.setattr(qd, "_drain_one_queue", lambda t: None)
    monkeypatch.setattr(qd, "_watch_limited_sessions", lambda: None)
    monkeypatch.setattr(qd, "_drain_mailboxes", lambda: None)
    pq.enqueue("orch", "next task")
    qd._WORKER_HOLD["ghost"] = {"since": T0 - qd._QUEUE_WORKER_HOLD_MAX, "key": ()}
    qd._WORKER_HOLD["orch"] = {"since": T0 - 60, "key": ()}
    try:
        qd._drain_prompt_queues()
        assert "ghost" not in qd._WORKER_HOLD
        assert qd._WORKER_HOLD["orch"]["since"] == T0 - 60
    finally:
        qd._WORKER_HOLD.pop("ghost", None)


# --------------------------------------------------------------------------- #
# The drain's dwell and the hold's announcements
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("marker", [True, False])
def test_the_full_settle_runs_again_after_the_hold_lifts(fl, marker):
    """A dwell already under way when the hold begins is wiped: the idle time
    spent waiting on workers never counts toward the next send."""
    server, st = fl
    _child("w1", "offline")
    _child("w2", "offline")
    pq.enqueue("orch", "next task")
    if marker:
        agent_state._ACTIVITY_CACHE["orch"] = {"reading": ("idle", "marker")}
    settle = qd._QUEUE_IDLE_SETTLE_MARKER if marker else qd._QUEUE_IDLE_SETTLE
    server._drain_one_queue("orch")  # no hold yet: the first sighting starts the dwell
    assert server._QUEUE_STATE["orch"]["idle_since"] == st.clock["now"]
    st.clock["now"] += 1
    _child("w1", "working")  # a worker comes up before the dwell settles
    _passes(server, st, "orch", n=4)  # well past the settle, but held
    assert _queue_typed(st, "orch") == []
    assert server._QUEUE_STATE["orch"]["idle_since"] is None
    _child("w1", "offline")  # the worker closed: the hold lifts
    server._drain_one_queue("orch")  # first pass after the release: dwell starts
    assert _queue_typed(st, "orch") == []
    released = st.clock["now"]
    assert server._QUEUE_STATE["orch"]["idle_since"] == released
    st.clock["now"] = released + settle - 0.5
    server._drain_one_queue("orch")
    assert _queue_typed(st, "orch") == []
    st.clock["now"] = released + settle
    server._drain_one_queue("orch")
    assert _queue_typed(st, "orch") == ["next task"]


def test_the_grace_after_a_report_expires_then_the_queue_sends(fl):
    server, st = fl
    _child("w1", "idle", since=st.clock["now"])
    _child("w2", "offline")
    _report("w1", now=st.clock["now"])
    mb.fetch("orch", mark_read=True)  # read via its inbox: no mail to type
    pq.enqueue("orch", "next task")
    assert qd.queue_hold("orch", st.clock["now"])["workers"] == {"w1": "reported"}
    _passes(server, st, "orch", n=4)  # 20 s, inside the grace
    assert _queue_typed(st, "orch") == []
    st.clock["now"] = T0 + qd._QUEUE_WORKER_GRACE
    server._drain_one_queue("orch")  # released; the dwell starts
    assert _queue_typed(st, "orch") == []
    st.clock["now"] += qd._QUEUE_IDLE_SETTLE
    server._drain_one_queue("orch")
    assert _queue_typed(st, "orch") == ["next task"]


def _hold_events(st, title="orch"):
    return [
        e["data"]
        for e in st.events
        if e["session"] == title and "waiting_on" in e["data"]
    ]


def test_hold_announcements_start_change_and_release_once(fl):
    server, st = fl
    _child("w1", "working")
    _child("w2", "offline")
    pq.enqueue("orch", "next task")
    _passes(server, st, "orch", n=3)
    (start,) = _hold_events(st)
    assert start == {
        "pending": 1,
        "enabled": True,
        "loop": False,
        "waiting_on": ["w1"],
        "waiting": "waiting on 1 worker (w1)",
    }
    # w1 reports: the key changes (working → reported, mail pending).
    _child("w1", "idle", since=st.clock["now"])
    _report("w1", now=st.clock["now"])
    _passes(server, st, "orch", n=2)
    events = _hold_events(st)
    assert len(events) == 2
    assert events[1]["waiting_on"] == ["w1"]
    assert events[1]["waiting"] == (
        "waiting on 1 worker (w1); 1 worker message to type first"
    )
    # The report is read: the mail half of the key changes once more.
    mb.fetch("orch", mark_read=True)
    _passes(server, st, "orch", n=2)
    assert len(_hold_events(st)) == 3
    # Past the grace: released, announced once, then quiet.
    st.clock["now"] += qd._QUEUE_WORKER_GRACE
    _passes(server, st, "orch", n=1)
    events = _hold_events(st)
    assert len(events) == 4
    assert events[-1]["waiting_on"] == [] and events[-1]["waiting"] == ""
    _passes(server, st, "orch", n=1)
    assert len(_hold_events(st)) == 4


@pytest.mark.parametrize(
    "reason, words",
    [
        (None, ""),
        ({}, ""),
        ({"workers": {"a": "working"}}, "waiting on 1 worker (a)"),
        (
            {"workers": {"b": "idle", "a": "working"}},
            "waiting on 2 workers (a, b)",
        ),
        ({"mail": 1}, "1 worker message to type first"),
        ({"mail": 3}, "3 worker messages to type first"),
        ({"inbox_wait": True}, "waiting on its inbox"),
        (
            {"workers": {"a": "working"}, "mail": 2, "inbox_wait": True},
            "waiting on 1 worker (a); 2 worker messages to type first; "
            "waiting on its inbox",
        ),
    ],
)
def test_hold_words(reason, words):
    assert qd._hold_words(reason) == words


# --------------------------------------------------------------------------- #
# The mailbox lane and Send now
# --------------------------------------------------------------------------- #
def test_a_mail_only_hold_lifts_once_the_lane_delivers(fl):
    """No unfinished child, only a child's pending message: the lane types it
    and the hold clears."""
    server, st = fl
    _child("w1", "offline")
    _child("w2", "offline")
    msg = mb.post("orch", "a question", sender="w1", now=st.clock["now"] - 120)
    pq.enqueue("orch", "next task")
    hold = qd.queue_hold("orch", st.clock["now"])
    assert hold["workers"] == {} and hold["mail"] == 1
    for _ in range(8):
        server._drain_one_mailbox("orch")
        st.clock["now"] += 5
    lines = [t for (who, t) in st.typed if who == "orch"]
    assert len(lines) == 1 and msg["id"] in lines[0]
    assert qd.queue_hold("orch", st.clock["now"]) is None
    assert server._queue_wants_turn("orch", st.clock["now"]) is True


def test_a_childless_session_keeps_queue_first_priority(fl):
    """No hold: a ready queue still goes before a pending message."""
    server, st = fl
    mb.post("solo", "fyi", sender="orch", now=st.clock["now"] - 120)
    pq.enqueue("solo", "task one")
    assert qd.queue_hold("solo", st.clock["now"]) is None
    assert server._queue_wants_turn("solo", st.clock["now"]) is True
    for _ in range(8):
        server._drain_one_mailbox("solo")
        st.clock["now"] += 5
    assert st.typed == []


def test_send_now_delivers_through_a_worker_hold(fl):
    from fastapi.testclient import TestClient

    server, st = fl
    _child("w1", "working")
    _child("w2", "offline")
    iid = pq.enqueue("orch", "next task")["items"][0]["id"]
    _passes(server, st, "orch", n=3)
    assert qd.queue_hold("orch", st.clock["now"]) is not None
    assert _queue_typed(st, "orch") == []  # the drain held it
    r = TestClient(server.app).post(
        "/api/instances/orch/queue/send_now", json={"id": iid}
    )
    assert r.status_code == 200, r.text
    assert _queue_typed(st, "orch") == ["next task"]
    assert pq.list_queue("orch") == []
    server._QUEUE_STATE.pop("orch", None)


# --------------------------------------------------------------------------- #
# agent_state: an idle marker under a live turn's status line
# --------------------------------------------------------------------------- #
_LIVE = "\n".join(
    [
        "⏺ Bash(git commit -q -F .git/COMMIT_EDITMSG)",
        "  ⎿  Running… (1m 24s)",
        "",
        "✽ Harmonizing… (3m 54s · ↓ 7.7k tokens · esc to interrupt)",
        "",
        "╭──────────────────────────────────────────╮",
        "│ >                                        │",
        "╰──────────────────────────────────────────╯",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ]
)
_QUOTED = "\n".join(
    ["⏺ The hint reads (esc to interrupt) while a turn runs."]
    + ["  line %d" % i for i in range(12)]
    + [
        "╭──────────────────────────────────────────╮",
        "│ >                                        │",
        "╰──────────────────────────────────────────╯",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ]
)


def test_turn_live_on_screen_reads_only_the_bottom_lines():
    from backend import providers

    claude = providers.resolve("claude")
    assert agent_state._turn_live_on_screen(claude, _LIVE) is True
    assert agent_state._turn_live_on_screen(claude, _QUOTED) is False
    assert agent_state._turn_live_on_screen(claude, "") is False
    assert agent_state._turn_live_on_screen(providers.resolve("bash"), _LIVE) is False


@pytest.fixture
def probe(monkeypatch):
    """``_agent_activity`` for a Claude session whose hook marker says idle."""
    import time

    from backend import providers
    from backend.web import server
    from backend.web.core import proc as proc_mod

    title = "qw-probe"
    agent_state._ACTIVITY_CACHE.pop(title, None)

    def run_capped(argv, **kw):
        if argv[:2] == ["tmux", "has-session"]:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        raise AssertionError("unexpected subprocess %r" % (argv,))

    monkeypatch.setattr(proc_mod, "run_capped", run_capped)
    monkeypatch.setattr(
        server, "_pane_meta", lambda n: ("claude", time.time() - 3600, 4242, "100x30")
    )
    monkeypatch.setattr(server, "_agent_exited", lambda n, c: False)
    monkeypatch.setattr(agent_state, "_maybe_record_thread", lambda *a: None)
    monkeypatch.setattr(agent_state, "_startup_trust_gate_clarify", lambda *a: False)
    monkeypatch.setattr(agent_state, "_limit_on_pane", lambda *a, **k: False)
    cls = type(providers.resolve("claude"))
    monkeypatch.setattr(cls, "activity_state", lambda self, n: "idle")
    monkeypatch.setattr(cls, "activity_state_age", lambda self, n: 1.0)

    class _Inst:
        Program = "claude"
        Status = Status.Running

        def Started(self):  # noqa: N802
            return True

    yield title, _Inst()
    agent_state._ACTIVITY_CACHE.pop(title, None)
    agent_state._LAST_SCREEN.pop(title, None)


def test_an_idle_marker_under_a_live_status_line_reads_working(probe):
    """A nested ``claude -p`` (a pre-commit hook's) wrote the session's idle
    marker mid-turn; the spinner on screen says the turn is live."""
    title, inst = probe
    assert agent_state._agent_activity(inst, title, screen=_LIVE) == "working"


def test_an_idle_marker_with_the_phrase_only_in_the_transcript_stays_idle(probe):
    title, inst = probe
    assert agent_state._agent_activity(inst, title, screen=_QUOTED) == "idle"


def test_an_idle_marker_on_a_limit_screen_never_reads_working(probe):
    """A usage-limit screen that still shows the status hint: the limit/idle
    path wins, never a 'working' that would arm turn-end evidence."""
    title, inst = probe
    screen = "Claude usage limit reached. Your limit resets 3pm.\n" + _LIVE
    assert agent_state.is_limit_screen(screen)
    assert agent_state._agent_activity(inst, title, screen=screen) != "working"
    assert agent_state.worked_at(title) is None


def test_an_idle_marker_with_no_capture_stays_idle(probe, monkeypatch):
    title, inst = probe
    monkeypatch.setattr(agent_state, "_capture_visible", lambda srv, name: None)
    assert agent_state._agent_activity(inst, title, screen=None) == "idle"


def test_a_dialog_on_screen_wins_over_the_live_status_line(probe, monkeypatch):
    title, inst = probe
    monkeypatch.setattr(agent_state, "_dialog_parsed", lambda p, text: True)
    assert agent_state._agent_activity(inst, title, screen=_LIVE) == "clarify"


def test_a_status_reading_arms_evidence_on_the_status_tiers(probe):
    """Pinned for the consumers: a 'status' working reading stamps the work
    evidence as 'status' (the 25 s turn-end dwell tier), and is NOT an
    authoritative reading (the announce settle skip / the queue's fast tier
    take their slow path on it)."""
    from backend.web.core import state_events

    title, inst = probe
    assert agent_state._agent_activity(inst, title, screen=_LIVE) == "working"
    rec = agent_state._ACTIVITY_CACHE[title]
    assert rec["reading"] == ("working", "status")
    assert agent_state.work_evidence(title) == "status"
    assert agent_state.worked_at(title) is not None
    assert agent_state.last_worked_at(title) == agent_state.worked_at(title)
    assert (
        state_events._TURN_END_DWELLS[agent_state.work_evidence(title)]
        == state_events._TURN_END_DWELL_STATUS_S
    )
    assert agent_state.reading_is_authoritative(title, "working") is False


def test_a_provider_without_a_dialog_parser_ignores_the_status_line(probe, monkeypatch):
    """A config provider with working patterns but no parser takes no screen:
    a caller's capture is dropped, no capture is taken, and its idle marker
    stands (unchanged behaviour)."""
    from backend import providers
    from backend.providers.generic import GenericProvider

    title, _ = probe
    goose = providers.resolve("goose")
    assert isinstance(goose, GenericProvider) and goose.working_pane_patterns()
    monkeypatch.setattr(GenericProvider, "parses_dialogs", lambda self: False)
    monkeypatch.setattr(GenericProvider, "activity_state", lambda self, n: "idle")
    monkeypatch.setattr(GenericProvider, "activity_state_age", lambda self, n: 1.0)
    captured = []
    monkeypatch.setattr(
        agent_state, "_capture_visible", lambda srv, name: captured.append(name)
    )

    class _Goose:
        Program = "goose"
        Status = Status.Running

        def Started(self):  # noqa: N802
            return True

    screen = "working on it\n  (Ctrl+C to interrupt)\n> "
    assert agent_state._turn_live_on_screen(goose, screen) is True
    assert agent_state._agent_activity(_Goose(), title, screen=screen) == "idle"
    assert captured == []


class _Pats:
    def __init__(self, pats=None, exc=None):
        self.pats, self.exc = pats, exc

    def working_pane_patterns(self):
        if self.exc:
            raise self.exc
        return self.pats


_BOX = ["╭────╮", "│ >  │", "╰────╯", "  footer"]


@pytest.mark.parametrize(
    "provider, screen, live",
    [
        (_Pats(exc=RuntimeError("boom")), _LIVE, False),
        (_Pats(()), _LIVE, False),
        (_Pats(None), _LIVE, False),
        (_Pats(("esc to (interrupt",)), _LIVE, False),  # re.error
        (_Pats(("esc to interrupt",)), "✽ Working… (ESC TO INTERRUPT)", True),
        # Trailing blank lines do not count toward the bottom lines.
        (
            _Pats(("esc to interrupt",)),
            "\n".join(["x"] * 5 + ["✽ Working… (esc to interrupt)"] + _BOX + [""] * 20),
            True,
        ),
        # The 8th non-blank line from the bottom is read; the 9th is not.
        (
            _Pats(("esc to interrupt",)),
            "\n".join(["esc to interrupt"] + ["line %d" % i for i in range(7)]),
            True,
        ),
        (
            _Pats(("esc to interrupt",)),
            "\n".join(["esc to interrupt"] + ["line %d" % i for i in range(8)]),
            False,
        ),
        # Blank lines in between are skipped, not counted.
        (
            _Pats(("esc to interrupt",)),
            "\n".join(["esc to interrupt"] + ["", "line", ""] * 7),
            True,
        ),
    ],
)
def test_turn_live_on_screen_edges(provider, screen, live):
    assert agent_state._LIVE_TURN_BOTTOM_LINES == 8
    assert agent_state._turn_live_on_screen(provider, screen) is live


def _rec():
    return {"reported": None, "state_since": None, "worked_at": None}


def test_verdict_records_status_evidence():
    rec = _rec()
    assert agent_state._verdict(rec, "working", 100.0, arms=True, source="status") == (
        "working"
    )
    assert rec["worked_evidence"] == "status"
    assert rec["worked_at"] == 100.0 and rec["last_worked_at"] == 100.0
    assert rec["reading"] == ("working", "status")


@pytest.mark.parametrize(
    "source, proof, evidence",
    [
        ("marker", "cpu", "marker"),
        ("pane", "status", "status"),
        ("pane", "cpu", "cpu"),
        ("pane", None, "cpu"),
    ],
)
def test_verdict_evidence_for_other_sources(source, proof, evidence):
    rec = dict(_rec(), proof=proof)
    agent_state._verdict(rec, "working", 100.0, arms=True, source=source)
    assert rec["worked_evidence"] == evidence


def test_verdict_status_without_arms_stamps_nothing():
    rec = _rec()
    agent_state._verdict(rec, "working", 100.0, source="status")
    assert rec["worked_at"] is None and "worked_evidence" not in rec
