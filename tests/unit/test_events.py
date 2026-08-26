"""Unit tests for the session event bus + user shell hooks
(:mod:`backend.web.core.events`, roadmap B1/B3) and the ``/api/events``
websocket stream (B2).

Hermetic: hook executables live in a pytest tmp dir (``MINDFLOCK_HOOKS_DIR``);
no real ~/.mindflock is ever read.
"""

from __future__ import annotations

import asyncio
import json
import re
import stat
import time

import pytest

from backend.web.core.events import (
    EVENT_NAMES,
    EventBus,
    hook_executables,
    run_shell_hooks,
)


# --------------------------------------------------------------------------- #
# EventBus: emit / subscribe / backlog
# --------------------------------------------------------------------------- #
def test_emit_returns_sequenced_envelope():
    bus = EventBus()
    env = bus.emit("session.created", session="s1", new="loading")
    assert env["seq"] == 1
    assert env["event"] == "session.created"
    assert env["session"] == "s1"
    assert env["old"] is None
    assert env["new"] == "loading"
    assert env["data"] == {}
    assert abs(env["ts"] - time.time()) < 5


def test_seq_is_monotonic():
    bus = EventBus()
    seqs = [bus.emit("session.created", session="s%d" % i)["seq"] for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]


def test_subscribe_receives_events_and_unsubscribe_stops_them():
    bus = EventBus()
    got = []
    unsubscribe = bus.subscribe(got.append)
    bus.emit("session.paused", session="a")
    assert [e["event"] for e in got] == ["session.paused"]
    unsubscribe()
    bus.emit("session.resumed", session="a")
    assert len(got) == 1


def test_failing_subscriber_does_not_break_emit_or_other_subscribers():
    bus = EventBus()
    got = []

    def bad(env):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(got.append)
    env = bus.emit("session.deleted", session="x")
    assert env["seq"] == 1
    assert len(got) == 1


def test_backlog_replays_and_since_filters():
    bus = EventBus()
    for i in range(4):
        bus.emit("session.status_changed", session="s", old=str(i), new=str(i + 1))
    assert [e["seq"] for e in bus.backlog()] == [1, 2, 3, 4]
    assert [e["seq"] for e in bus.backlog(since=2)] == [3, 4]
    assert bus.backlog(since=99) == []


def test_ring_buffer_caps_history():
    bus = EventBus(history=3)
    for i in range(10):
        bus.emit("session.activity_changed", session="s", new=str(i))
    log = bus.backlog()
    assert len(log) == 3
    assert [e["seq"] for e in log] == [8, 9, 10]  # oldest dropped, seq still global


def test_event_names_vocabulary_is_complete():
    assert set(EVENT_NAMES) == {
        "session.created",
        "session.deleted",
        "session.paused",
        "session.resumed",
        "session.status_changed",
        "session.activity_changed",
        "session.stage_changed",
        "session.pr_state_changed",
        "session.budget_exceeded",
        "session.prompt_sent",
        "session.queue_changed",
        "session.usage_restored",
        "session.turn_ended",
        "session.autopilot_changed",
        "session.pushed",
        "session.test_plan_ready",
        "session.test_plan_failed",
        "session.test_plan_checked",
        "session.test_plan_gave_up",
        "session.test_plan_due",
    }


def test_the_turn_boundary_event_is_documented_for_extension_authors():
    """``docs/extensions.md`` is the contract an addon/hook author codes
    against, and this event exists precisely so nobody keys "the agent is done"
    off the raw idle flip. A row nobody can find leaves them writing the bug
    the event was added to fix, so the doc has to carry both halves: the
    vocabulary row and the recipe that steers them away from the old signal.
    """
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "extensions.md").read_text(
        encoding="utf-8"
    )
    rows = [ln for ln in doc.splitlines() if ln.startswith("| `session.")]
    documented = {
        name for ln in rows for name in re.findall(r"`(session\.[a-z_]+)`", ln)
    }
    assert "session.turn_ended" in documented
    assert "session.turn_ended" in EVENT_NAMES
    # The recipe list, where "agent has finished" is spelled out — including
    # which event it is NOT.
    assert "**agent has finished**: `session.turn_ended`" in doc


# --------------------------------------------------------------------------- #
# Shell hooks (B3)
# --------------------------------------------------------------------------- #
@pytest.fixture
def hooks_root(tmp_path, monkeypatch):
    root = tmp_path / "hooks"
    monkeypatch.setenv("MINDFLOCK_HOOKS_DIR", str(root))
    return root


def _write_hook(dirpath, name, script):
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_text("#!/bin/sh\n" + script)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_hook_executables_lists_event_and_all_dirs(hooks_root):
    a = _write_hook(hooks_root / "session.created", "10-a.sh", "true")
    b = _write_hook(hooks_root / "all", "20-b.sh", "true")
    # Non-executable files are skipped.
    nx = hooks_root / "session.created" / "not-exec.sh"
    nx.write_text("#!/bin/sh\ntrue\n")
    got = hook_executables("session.created")
    assert got == [a, b]


def test_hook_executables_missing_dirs_is_empty(hooks_root):
    assert hook_executables("session.deleted") == []


def test_run_shell_hooks_passes_env_and_stdin(hooks_root, tmp_path):
    out = tmp_path / "captured"
    _write_hook(
        hooks_root / "session.clarify_test",
        "capture.sh",
        'printf \'%%s|%%s|%%s|%%s|\' "$MINDFLOCK_EVENT" "$MINDFLOCK_SESSION" '
        '"$MINDFLOCK_OLD" "$MINDFLOCK_NEW" > %s; cat >> %s\n' % (out, out),
    )
    envelope = {
        "seq": 7,
        "event": "session.clarify_test",
        "session": "sess-1",
        "old": "working",
        "new": "clarify",
        "ts": 123.0,
        "data": {},
    }
    asyncio.run(run_shell_hooks(envelope))
    head, _, payload = out.read_text().rpartition("|")
    assert head == "session.clarify_test|sess-1|working|clarify"
    assert json.loads(payload) == envelope


def test_run_shell_hooks_failure_is_swallowed(hooks_root):
    _write_hook(hooks_root / "all", "fails.sh", "exit 3")
    envelope = {
        "seq": 1,
        "event": "session.deleted",
        "session": "s",
        "old": None,
        "new": None,
        "ts": 0.0,
        "data": {},
    }
    asyncio.run(run_shell_hooks(envelope))  # must not raise


def test_run_shell_hooks_none_values_map_to_empty_env(hooks_root, tmp_path):
    out = tmp_path / "envs"
    _write_hook(
        hooks_root / "session.created",
        "env.sh",
        'printf \'[%%s][%%s]\' "$MINDFLOCK_OLD" "$MINDFLOCK_NEW" > %s' % (out,),
    )
    envelope = {
        "seq": 1,
        "event": "session.created",
        "session": "s",
        "old": None,
        "new": None,
        "ts": 0.0,
        "data": {},
    }
    asyncio.run(run_shell_hooks(envelope))
    assert out.read_text() == "[][]"


def test_emit_dispatches_hooks_without_a_running_loop(hooks_root, tmp_path):
    # emit() from a plain sync context (no asyncio loop) still runs hooks —
    # on a throwaway thread — and never blocks or raises.
    out = tmp_path / "fired"
    _write_hook(hooks_root / "session.paused", "fire.sh", "echo yes > %s" % out)
    bus = EventBus()
    bus.emit("session.paused", session="s")
    deadline = time.time() + 5
    while not out.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert out.read_text().strip() == "yes"


# --------------------------------------------------------------------------- #
# /api/events websocket (B2)
# --------------------------------------------------------------------------- #
def _recv_hello(ws) -> dict:
    """Consume + sanity-check the initial hello frame (L4: sent before the
    backlog so clients can compare envelope ts against the server's clock)."""
    hello = ws.receive_json()
    assert hello["event"] == "hello"
    assert hello["seq"] == 0
    assert isinstance(hello["server_time"], float)
    assert hello["server_time"] == hello["ts"]
    return hello


def test_events_ws_backlog_and_live_stream(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from backend.web import server
    from backend.web.core import events as events_mod

    monkeypatch.setenv("MINDFLOCK_HOOKS_DIR", str(tmp_path / "no-hooks"))
    # A fresh bus so this test doesn't see events other tests emitted.
    bus = EventBus()
    # server._events IS this module object, so one setattr patches both views.
    monkeypatch.setattr(events_mod, "BUS", bus)
    assert server._events.BUS is bus
    # Connecting starts the F6 state tick — keep it out of this test's stream.
    monkeypatch.setattr(server, "_tick_state_changes", lambda: None)

    first = bus.emit("session.created", session="ws-a", new="loading")
    second = bus.emit(
        "session.status_changed", session="ws-a", old="loading", new="running"
    )

    client = TestClient(server.app)
    with client.websocket_connect("/api/events?since=%d" % first["seq"]) as ws:
        _recv_hello(ws)
        env = ws.receive_json()
        assert env["seq"] == second["seq"]
        assert env["event"] == "session.status_changed"
        assert env["old"] == "loading" and env["new"] == "running"
        # A live event emitted after connect is pushed through.
        bus.emit("session.activity_changed", session="ws-a", old="working", new="idle")
        env = ws.receive_json()
        assert env["event"] == "session.activity_changed"
        assert env["session"] == "ws-a"
        assert set(env) == {"seq", "event", "session", "old", "new", "ts", "data"}


def test_events_ws_without_since_replays_full_backlog(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from backend.web import server
    from backend.web.core import events as events_mod

    monkeypatch.setenv("MINDFLOCK_HOOKS_DIR", str(tmp_path / "no-hooks"))
    bus = EventBus()
    # server._events IS this module object, so one setattr patches both views.
    monkeypatch.setattr(events_mod, "BUS", bus)
    assert server._events.BUS is bus
    # Connecting starts the F6 state tick — keep it out of this test's stream.
    monkeypatch.setattr(server, "_tick_state_changes", lambda: None)

    for i in range(3):
        bus.emit("session.stage_changed", session="ws-b", new="stage-%d" % i)

    client = TestClient(server.app)
    with client.websocket_connect("/api/events") as ws:
        _recv_hello(ws)
        got = [ws.receive_json() for _ in range(3)]
        assert [e["seq"] for e in got] == [1, 2, 3]
        assert all(e["session"] == "ws-b" for e in got)


# --------------------------------------------------------------------------- #
# F6: first-transition seeding + the /api/events state tick
# --------------------------------------------------------------------------- #
@pytest.fixture
def _state_env(monkeypatch, tmp_path):
    """A fresh bus + empty diff snapshot for the *_changed state helpers."""
    from backend.web import server
    from backend.web.core import events as events_mod

    monkeypatch.setenv("MINDFLOCK_HOOKS_DIR", str(tmp_path / "no-hooks"))
    bus = EventBus()
    monkeypatch.setattr(events_mod, "BUS", bus)
    server._EVENT_SNAPSHOT.clear()
    yield server, bus
    server._EVENT_SNAPSHOT.clear()


def test_seeded_snapshot_emits_first_transition(_state_env):
    # Creation seeds loading/offline/provisioning, so the FIRST real state
    # computation emits its transitions instead of only seeding the diff.
    server, bus = _state_env
    got = []
    bus.subscribe(got.append)
    server._seed_event_snapshot("t1")
    assert got == []  # seeding itself announces nothing
    server._emit_state_changes("t1", "running", "working", "agent")
    by_event = {e["event"]: e for e in got}
    assert by_event["session.status_changed"]["old"] == "loading"
    assert by_event["session.status_changed"]["new"] == "running"
    assert by_event["session.activity_changed"]["old"] == "offline"
    assert by_event["session.activity_changed"]["new"] == "working"
    assert by_event["session.stage_changed"]["old"] == "provisioning"
    assert by_event["session.stage_changed"]["new"] == "agent"


def test_seed_does_not_clobber_existing_snapshot(_state_env):
    server, bus = _state_env
    server._emit_state_changes("t2", "running", "working", "agent")  # first sighting
    server._seed_event_snapshot("t2")  # e.g. a re-create race — must not reset
    got = []
    bus.subscribe(got.append)
    server._emit_state_changes("t2", "running", "working", "agent")
    assert got == []  # unchanged state stays silent (no fake loading->running)


def test_concurrent_poll_and_tick_emit_transition_once(_state_env):
    # The HTTP poll and the events tick computing the SAME transition at once
    # must emit it exactly once — the locked snapshot diff is the dedupe.
    import threading

    server, bus = _state_env
    server._seed_event_snapshot("t3")
    got = []
    bus.subscribe(got.append)
    barrier = threading.Barrier(2)

    def compute():
        barrier.wait()
        server._emit_state_changes("t3", "running", "working", "agent")

    threads = [threading.Thread(target=compute) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    activity = [e for e in got if e["event"] == "session.activity_changed"]
    assert len(activity) == 1


@pytest.mark.parametrize("state", ["idle", "clarify", "limit"])
def test_activity_flicker_emits_nothing(_state_env, monkeypatch, state):
    # One tick misreads a busy pane as idle/clarify/limit; the next reads
    # working again. The settle window must swallow the whole excursion —
    # this is the "notifications over-trigger on a flicker" fix.
    server, bus = _state_env
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 60.0)
    server._emit_state_changes("f1", "running", "working", "agent")  # seed
    got = []
    bus.subscribe(got.append)
    server._emit_state_changes("f1", "running", state, "agent")  # flicker in…
    server._emit_state_changes("f1", "running", "working", "agent")  # …and out
    assert [e for e in got if e["event"] == "session.activity_changed"] == []


def test_activity_settles_then_emits_once(_state_env, monkeypatch):
    # A reading that persists past the settle window emits exactly one
    # transition, carrying the last CONFIRMED old value.
    server, bus = _state_env
    # 0.0 keeps the two-sighting semantics without sleeping: the first
    # sighting parks the candidate, the second (age >= 0) settles it.
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 0.0)
    server._emit_state_changes("f2", "running", "working", "agent")  # seed
    got = []
    bus.subscribe(got.append)
    server._emit_state_changes("f2", "running", "idle", "agent")  # parks
    assert [e for e in got if e["event"] == "session.activity_changed"] == []
    server._emit_state_changes("f2", "running", "idle", "agent")  # settles
    acts = [e for e in got if e["event"] == "session.activity_changed"]
    assert len(acts) == 1
    assert acts[0]["old"] == "working" and acts[0]["new"] == "idle"
    server._emit_state_changes("f2", "running", "idle", "agent")  # no repeat
    assert len([e for e in got if e["event"] == "session.activity_changed"]) == 1


def test_activity_leaving_settled_state_is_instant(_state_env, monkeypatch):
    # Only transitions INTO the flicker-prone states are held; the agent
    # starting to work again announces immediately.
    server, bus = _state_env
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 0.0)
    server._emit_state_changes("f3", "running", "working", "agent")  # seed
    server._emit_state_changes("f3", "running", "idle", "agent")
    server._emit_state_changes("f3", "running", "idle", "agent")  # settled idle
    got = []
    bus.subscribe(got.append)
    server._emit_state_changes("f3", "running", "working", "agent")
    acts = [e for e in got if e["event"] == "session.activity_changed"]
    assert len(acts) == 1
    assert acts[0]["old"] == "idle" and acts[0]["new"] == "working"


def test_activity_candidate_switch_restarts_settle(_state_env, monkeypatch):
    # working -> (pending idle) -> clarify: the clarify candidate replaces the
    # idle one and starts its own window — the abandoned idle never emits.
    server, bus = _state_env
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 0.0)
    server._emit_state_changes("f4", "running", "working", "agent")  # seed
    got = []
    bus.subscribe(got.append)
    server._emit_state_changes("f4", "running", "idle", "agent")  # parks idle
    server._emit_state_changes("f4", "running", "clarify", "agent")  # replaces
    assert [e for e in got if e["event"] == "session.activity_changed"] == []
    server._emit_state_changes("f4", "running", "clarify", "agent")  # settles
    acts = [e for e in got if e["event"] == "session.activity_changed"]
    assert len(acts) == 1
    assert acts[0]["old"] == "working" and acts[0]["new"] == "clarify"


def test_boot_quiet_window_swallows_state_events(_state_env, monkeypatch):
    # Inside the post-launch quiet window nothing announces — the boot burst
    # ("here is the standing state of every session") is exactly what this
    # kills — but the snapshot still tracks the truth underneath.
    server, bus = _state_env
    monkeypatch.setattr(server, "_BOOT_QUIET_SECONDS", 60.0)
    monkeypatch.setattr(server, "_BOOT_MONO", time.monotonic())
    got = []
    bus.subscribe(got.append)
    server._seed_event_snapshot("b1")
    server._emit_state_changes("b1", "running", "working", "agent")
    assert got == []
    # Window over: the NEXT transition announces, diffed against the state
    # recorded (silently) during the window — old is "working", not the seed.
    monkeypatch.setattr(server, "_BOOT_QUIET_SECONDS", 0.0)
    server._emit_state_changes("b1", "running", "offline", "agent")
    acts = [e for e in got if e["event"] == "session.activity_changed"]
    assert len(acts) == 1
    assert acts[0]["old"] == "working" and acts[0]["new"] == "offline"


def test_events_ws_tick_streams_changes_without_http_poll(monkeypatch, tmp_path):
    # A WS-only consumer (no /api/instances poller anywhere) still receives
    # *_changed events: connecting starts the tick, which recomputes state.
    from fastapi.testclient import TestClient

    from backend.web import server
    from backend.web.core import events as events_mod

    monkeypatch.setenv("MINDFLOCK_HOOKS_DIR", str(tmp_path / "no-hooks"))
    bus = EventBus()
    monkeypatch.setattr(events_mod, "BUS", bus)
    server._EVENT_SNAPSHOT.clear()
    monkeypatch.setattr(server, "_EVENTS_TICK_INTERVAL", 0.05)

    ticks = {"n": 0}

    def fake_tick():
        ticks["n"] += 1
        server._emit_state_changes("tick-sess", "running", "working", "agent")

    monkeypatch.setattr(server, "_tick_state_changes", fake_tick)
    server._seed_event_snapshot("tick-sess")

    client = TestClient(server.app)
    try:
        with client.websocket_connect("/api/events") as ws:
            assert server._EVENTS_WS_CLIENTS == 1
            _recv_hello(ws)
            got = [ws.receive_json() for _ in range(3)]
            events = {e["event"] for e in got}
            assert events == {
                "session.status_changed",
                "session.activity_changed",
                "session.stage_changed",
            }
            act = next(e for e in got if e["event"] == "session.activity_changed")
            assert act["old"] == "offline" and act["new"] == "working"
        # Last client gone -> the ticker ends itself (bounded wait).
        deadline = time.time() + 5
        while time.time() < deadline:
            task = server._EVENTS_TICK_TASK
            if server._EVENTS_WS_CLIENTS == 0 and task is not None and task.done():
                break
            time.sleep(0.02)
        assert server._EVENTS_WS_CLIENTS == 0
        assert server._EVENTS_TICK_TASK is not None and server._EVENTS_TICK_TASK.done()
        # And it really stopped ticking.
        n = ticks["n"]
        time.sleep(0.2)
        assert ticks["n"] == n
    finally:
        server._EVENT_SNAPSHOT.clear()


def test_events_ws_tick_no_duplicates_with_concurrent_http_poll(monkeypatch, tmp_path):
    # Tick running + an /api/instances-style computation racing it: each
    # transition arrives at the WS exactly once (shared snapshot dedupe).
    from fastapi.testclient import TestClient

    from backend.web import server
    from backend.web.core import events as events_mod

    monkeypatch.setenv("MINDFLOCK_HOOKS_DIR", str(tmp_path / "no-hooks"))
    bus = EventBus()
    monkeypatch.setattr(events_mod, "BUS", bus)
    server._EVENT_SNAPSHOT.clear()
    monkeypatch.setattr(server, "_EVENTS_TICK_INTERVAL", 0.05)

    def fake_tick():
        server._emit_state_changes("dup-sess", "running", "working", "agent")

    monkeypatch.setattr(server, "_tick_state_changes", fake_tick)
    server._seed_event_snapshot("dup-sess")

    client = TestClient(server.app)
    try:
        with client.websocket_connect("/api/events") as ws:
            _recv_hello(ws)
            # Simulate the HTTP poll handler computing the same state in a
            # worker thread while ticks fire.
            server._emit_state_changes("dup-sess", "running", "working", "agent")
            got = [ws.receive_json() for _ in range(3)]
            # Give any duplicate a chance to arrive, then drain-check via a
            # sentinel: emit a unique event and assert it comes NEXT.
            bus.emit("session.paused", session="sentinel")
            nxt = ws.receive_json()
            assert nxt["event"] == "session.paused"
            assert sorted(e["event"] for e in got) == [
                "session.activity_changed",
                "session.stage_changed",
                "session.status_changed",
            ]
    finally:
        server._EVENT_SNAPSHOT.clear()
