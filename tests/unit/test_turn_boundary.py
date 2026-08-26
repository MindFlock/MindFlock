"""Turn boundaries: when MindFlock is allowed to say "your agent has finished".

The complaint these pin down: *"we go from the offline state and I click it, we
go to running for a moment and then idle so it always triggers"*, and *"very
short periods of idleness trigger the notification"*.

Three separate facts had been collapsed into one event. ``activity_changed
new="idle"`` is a chip colour — it fires at the end of every assistant turn, on
a session that merely booted to a bare prompt, between two prompts of a
draining queue, and for a window that was just re-opened. ``session.turn_ended``
is the notification-grade fact, and it asserts all three of: the agent was
observed working in THIS tmux incarnation, it has been idle continuously since,
and nothing is queued to wake it back up.

Split across two levels: the state layer's provenance (does the record know the
agent worked?) and the emit layer's gate (does that provenance, plus a dwell,
plus an empty queue, produce the event?).
"""

from __future__ import annotations

import types

import pytest

from backend.web import server
from backend.web.core import agent_state
from backend.web.core import events as events_mod
from backend.web.core.events import EventBus


# --------------------------------------------------------------------------- #
# The state layer: incarnation-scoped provenance
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_record():
    agent_state._ACTIVITY_CACHE.pop("s", None)
    agent_state._LIMIT_PROBE.pop("s", None)
    yield
    agent_state._ACTIVITY_CACHE.pop("s", None)
    agent_state._LIMIT_PROBE.pop("s", None)


def test_working_stamps_work_evidence_and_idle_does_not():
    rec = agent_state._activity_record("s", 100.0)
    assert agent_state.worked_at("s") is None
    agent_state._verdict(rec, "idle", 1000.0)
    assert agent_state.worked_at("s") is None, "a quiet session has no work behind it"
    agent_state._verdict(rec, "working", 1010.0)
    assert agent_state.worked_at("s") == 1010.0
    agent_state._verdict(rec, "idle", 1020.0)
    assert agent_state.worked_at("s") == 1010.0, "idling does not erase the evidence"


def test_clarify_alone_is_not_work():
    # A trust/MCP gate on a brand-new session reports 'clarify' before the agent
    # has been handed anything to do. That must not arm a turn-end.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "clarify", 1000.0)
    assert agent_state.worked_at("s") is None


def test_state_since_tracks_the_reported_value():
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0)
    assert agent_state.state_since("s") == 1000.0
    agent_state._verdict(rec, "working", 1004.0)  # unchanged: clock does not move
    assert agent_state.state_since("s") == 1000.0
    agent_state._verdict(rec, "idle", 1008.0)
    assert agent_state.state_since("s") == 1008.0


def test_a_new_tmux_incarnation_resets_the_record():
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0)
    rec["hash"] = "deadbeef"
    agent_state._LIMIT_PROBE["s"] = {"at": 1000.0, "limit": True}
    # Same session_created -> the record is the same object, history intact.
    assert agent_state._activity_record("s", 100.0) is rec
    assert agent_state.worked_at("s") == 1000.0
    # A different session_created is a different agent process: its work, its
    # pane baseline and its limit verdict all belonged to something that no
    # longer exists.
    fresh = agent_state._activity_record("s", 200.0)
    assert agent_state.worked_at("s") is None
    assert fresh.get("hash") is None
    assert "s" not in agent_state._LIMIT_PROBE


def test_a_stampless_first_sighting_is_not_kept():
    # `tmux display-message` can fail while `has-session` succeeds, so
    # `_pane_meta` answers all-None and there is no incarnation to file a
    # reading under. Such a record is a throwaway: keeping it would leave the
    # next real stamp to either adopt a dead run's history or discard it, and
    # discarding it a poll earlier is the same answer without the ambiguity.
    rec = agent_state._activity_record("s", None)
    agent_state._verdict(rec, "working", 1000.0)
    assert "s" not in agent_state._ACTIVITY_CACHE
    assert agent_state.worked_at("s") is None


def test_limit_spends_the_work_evidence():
    # A turn the usage cap cut short is not a turn that finished. The reading
    # only says "limit" while the banner is on the pane, so without dropping the
    # evidence here the session announces "has finished" the moment the pane
    # redraws — for work that was abandoned mid-thought.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0)
    assert agent_state.worked_at("s") == 1000.0
    agent_state._verdict(rec, "limit", 1010.0)
    assert agent_state.worked_at("s") is None
    agent_state._verdict(rec, "idle", 1020.0)
    assert agent_state.worked_at("s") is None, "a redrawn prompt must not re-arm it"
    # A resume re-earns it, so the genuine ending still announces.
    agent_state._verdict(rec, "working", 1030.0)
    assert agent_state.worked_at("s") == 1030.0


def test_claim_work_is_once_only():
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0)
    assert agent_state.claim_work("s") == 1000.0
    assert agent_state.claim_work("s") is None
    assert agent_state.claim_work("never-seen") is None


def test_a_record_with_no_stamp_adopts_one_rather_than_resetting():
    # Hand-seeded records (tests, and the very first sighting) must not be
    # treated as a stale incarnation.
    agent_state._ACTIVITY_CACHE["s"] = {"worked_at": 999.0}
    rec = agent_state._activity_record("s", 100.0)
    assert rec["created"] == 100.0
    assert agent_state.worked_at("s") == 999.0


def test_offline_no_longer_wipes_the_record():
    # The pop that used to live here is what guaranteed the phantom: a session
    # whose tmux was briefly gone came back with no history at all, straight
    # into the pane layer's first-sighting branch.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["tmux", "has-session"]:
            return types.SimpleNamespace(returncode=1, stdout=b"")
        pytest.fail("nothing else should run for a dead session: %r" % (cmd,))

    inst = types.SimpleNamespace(
        Title="s", Program="claude", Status="running", Started=lambda: True
    )
    import unittest.mock as mock

    with mock.patch.object(server.subprocess, "run", fake_run):
        with mock.patch.object(server.tmux, "to_mindflock_tmux_name", lambda t: t):
            assert server._agent_activity(inst, "s") == "offline"
    assert agent_state.worked_at("s") == 1000.0
    assert "s" in agent_state._ACTIVITY_CACHE


# --------------------------------------------------------------------------- #
# The emit layer: dwell + provenance + queue
# --------------------------------------------------------------------------- #
@pytest.fixture()
def bus(monkeypatch):
    b = EventBus()
    monkeypatch.setattr(events_mod, "BUS", b)
    server._EVENT_SNAPSHOT.clear()
    # The boot quiet window swallows everything for 30s after process start,
    # which in a test run is always.
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: False)
    monkeypatch.setattr(server._prompt_queue, "peek_next", lambda title: None)
    # 0.0 keeps the settle's two-sighting semantics (first call parks the
    # candidate, second adopts it) without any test having to sleep. The dwell
    # under test here is the one ABOVE the settle, not the settle itself.
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 0.0)
    # Work evidence that is always there to be claimed, so each test below
    # exercises the gate it names rather than the once-per-cycle claim (which
    # has its own test up in the state-layer section).
    monkeypatch.setattr(server._agent_state, "claim_work", lambda t: 5.0)
    yield b
    server._EVENT_SNAPSHOT.clear()


def _ended(got):
    return [e for e in got if e["event"] == "session.turn_ended"]


def _tick(title, activity="idle"):
    server._emit_state_changes(title, "running", activity, "agent")


def test_turn_ended_needs_observed_work(bus, monkeypatch):
    """THE OFFLINE-CLICK FIX.

    Clicking a session whose tmux has died relaunches its CLI, so the fresh
    incarnation parks at an empty prompt and reads idle. No amount of sitting
    there may produce "your agent has finished" — it never started.
    """
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: None)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    _tick("phantom", "offline")
    for _ in range(10):
        _tick("phantom", "idle")
    assert _ended(got) == []
    # …and the raw chip event still went out, so the UI is not blinded.
    assert [e["new"] for e in got if e["event"] == "session.activity_changed"] == [
        "idle"
    ]


def test_turn_ended_emits_once_per_work_cycle(bus, monkeypatch):
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    worked = {"at": 5.0}
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: worked["at"])

    def _claim(t):
        got, worked["at"] = worked["at"], None
        return got

    monkeypatch.setattr(server._agent_state, "claim_work", _claim)
    _tick("s1", "working")
    _tick("s1", "idle")  # parks the settle candidate
    _tick("s1", "idle")  # settles -> activity_changed, and the dwell is 0
    assert len(_ended(got)) == 1
    assert _ended(got)[0]["session"] == "s1"
    # Spending the evidence is the dedupe: it stays idle, it stays quiet.
    for _ in range(5):
        _tick("s1", "idle")
    assert len(_ended(got)) == 1


def test_a_short_idle_never_announces(bus, monkeypatch):
    """THE SHORT-IDLE FIX: the gap between two turns is not a finished session."""
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    _tick("s2", "working")
    clock["t"] += 4
    _tick("s2", "idle")
    clock["t"] += 4
    _tick("s2", "idle")  # settles the chip
    assert [e["new"] for e in got if e["event"] == "session.activity_changed"] == [
        "idle"
    ]
    # A whole half-minute of quiet — the Stop hook fires here after EVERY turn —
    # and still nothing claims the work is over.
    for _ in range(7):
        clock["t"] += 4
        _tick("s2", "idle")
    assert _ended(got) == []
    # Past the dwell, it finally is.
    clock["t"] += server._TURN_END_DWELL_S
    _tick("s2", "idle")
    assert len(_ended(got)) == 1


def test_work_resuming_restarts_the_dwell(bus, monkeypatch):
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    _tick("s3", "working")
    clock["t"] += 4
    _tick("s3", "idle")
    clock["t"] += 4
    _tick("s3", "idle")  # chip settles; dwell starts here
    clock["t"] += server._TURN_END_DWELL_S - 5
    _tick("s3", "working")  # the agent picked something up again
    clock["t"] += 4
    _tick("s3", "idle")
    clock["t"] += 4
    _tick("s3", "idle")
    clock["t"] += server._TURN_END_DWELL_S - 5  # would have been enough before
    _tick("s3", "idle")
    assert _ended(got) == []


def test_fresh_work_evidence_holds_the_announcement(bus, monkeypatch):
    """The dwell is asked of the EVIDENCE's clock too, not just the chip's.

    The drain and the autopilot driver both probe activity UNCACHED on their own
    5s cadences and never feed a snapshot, so a session idle for ten minutes
    that picks up a queued prompt can have second-old work evidence while the
    tickers still serve "idle" out of the 2.5s probe memo. Without this gate the
    announcement lands at the moment the agent STARTED a turn.
    """
    got = []
    bus.subscribe(got.append)
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    # The chip has said idle for ten minutes…
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    _tick("s7", "working")
    clock["t"] += 4
    _tick("s7", "idle")
    clock["t"] += 600
    _tick("s7", "idle")
    # …but the agent was seen working one second ago by an uncached probe.
    fresh = clock["t"] - 1.0
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: fresh)
    _tick("s7", "idle")
    assert _ended(got) == []
    # Once BOTH clocks agree the quiet has held, it announces.
    clock["t"] += server._TURN_END_DWELL_S + 1
    _tick("s7", "idle")
    assert len(_ended(got)) == 1


def test_a_queued_run_is_not_a_finished_run(bus, monkeypatch):
    # The drain feeds the next prompt once idle has held _QUEUE_IDLE_SETTLE.
    # Announcing "finished" in that gap is how a ten-prompt queue produced ten
    # notifications.
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    monkeypatch.setattr(
        server._prompt_queue, "peek_next", lambda title: {"id": "1", "text": "next"}
    )
    _tick("s4", "working")
    for _ in range(5):
        _tick("s4", "idle")
    assert _ended(got) == []


def test_limit_is_not_a_turn_ending(bus, monkeypatch):
    # A turn the account's usage cap cut short is not a turn that finished; the
    # default-on usage_limit rule is what speaks for that.
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    _tick("s5", "working")
    for _ in range(5):
        _tick("s5", "limit")
    assert _ended(got) == []


def test_boot_quiet_covers_turn_ends_too(bus, monkeypatch):
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: True)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    _tick("s6", "working")
    for _ in range(5):
        _tick("s6", "idle")
    assert _ended(got) == []


def test_dwell_exceeds_every_other_idle_settle_in_the_app():
    """The ordering these numbers have to keep, pinned so a future tweak of any
    one of them cannot silently invert it.

    The queue drains, and autopilot decides the agent is done, BEFORE anyone is
    told the work finished. Announcing first would mean telling the user a
    session was over and then watching it start typing again.
    """
    from backend.web.core import autopilot

    assert server._TURN_END_DWELL_S > server._QUEUE_IDLE_SETTLE
    assert server._TURN_END_DWELL_S > autopilot.IDLE_SETTLE_S
    # And it must dwarf the flicker filter, which answers a different question
    # ("did two probes agree?") and cannot answer this one.
    assert server._TURN_END_DWELL_S > server._ACTIVITY_SETTLE_SECONDS * 10


def test_clarify_after_work_keeps_the_EARLIER_stamp():
    # `clarify` is not work, and it is not "no work either" — it must leave the
    # evidence exactly as it found it. Refreshing it here would restart the
    # dwell every time a permission prompt redrew, and clearing it would lose a
    # genuine turn to a mid-turn confirmation.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0)
    agent_state._verdict(rec, "clarify", 1200.0)
    assert agent_state.worked_at("s") == 1000.0
    agent_state._verdict(rec, "idle", 1210.0)
    assert agent_state.worked_at("s") == 1000.0


def test_a_garbage_incarnation_stamp_does_not_retire_the_record():
    # `_pane_meta` parses tmux's own output; a truncated or mangled line yields
    # something that is not a number. Unparseable is UNKNOWN, and unknown must
    # not read as "a different session" — that would discard a live run's
    # history on a single bad poll.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0)
    for junk in ("not-a-number", object(), [1, 2]):
        assert agent_state._activity_record("s", junk) is rec
        assert agent_state.worked_at("s") == 1000.0
    assert rec["created"] == 100.0


def test_working_and_offline_readings_never_announce(bus, monkeypatch):
    """Only a SETTLED idle is a candidate. ``limit`` has its own test above;
    these are the other two the gate has to refuse."""
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    _tick("s8", "working")
    for _ in range(5):
        _tick("s8", "working")
    assert _ended(got) == []
    # A session whose tmux went away mid-turn has not finished it — it has
    # stopped being observable, which is a different sentence.
    for _ in range(5):
        _tick("s8", "offline")
    assert _ended(got) == []


def test_the_dwell_runs_from_what_users_were_TOLD(bus, monkeypatch):
    """``activity_at`` stamps the ANNOUNCED value, not the raw reading.

    The settle can hold an idle back for seconds before anyone hears about it.
    Measuring the dwell from the reading would spend that hold twice — once
    suppressing the flicker, once counting toward the announcement — and the
    "it has been quiet for 45 seconds" claim would be short by however long the
    settle took.
    """
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 3.0)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])

    _tick("s9", "working")  # first sighting: seeds the snapshot
    assert server._EVENT_SNAPSHOT["s9"]["activity_at"] == 1000.0
    clock["t"] = 1002.0
    _tick("s9", "working")  # unchanged -> the stamp is carried, not refreshed
    assert server._EVENT_SNAPSHOT["s9"]["activity_at"] == 1000.0

    clock["t"] = 1010.0
    _tick("s9", "idle")  # parked behind the settle: still announcing "working"
    assert server._EVENT_SNAPSHOT["s9"]["activity"] == "working"
    assert server._EVENT_SNAPSHOT["s9"]["activity_at"] == 1000.0
    clock["t"] = 1012.0
    _tick("s9", "idle")  # inside the settle window: still nothing announced
    assert server._EVENT_SNAPSHOT["s9"]["activity_at"] == 1000.0

    clock["t"] = 1014.0
    _tick("s9", "idle")  # settled -> announced HERE, not at 1010
    assert server._EVENT_SNAPSHOT["s9"]["activity"] == "idle"
    assert server._EVENT_SNAPSHOT["s9"]["activity_at"] == 1014.0

    clock["t"] = 1014.0 + server._TURN_END_DWELL_S - 0.5
    _tick("s9", "idle")
    assert _ended(got) == [], "the settle's four seconds are not the user's"
    clock["t"] = 1014.0 + server._TURN_END_DWELL_S + 0.5
    _tick("s9", "idle")
    assert len(_ended(got)) == 1
    assert _ended(got)[0]["data"]["idle_for"] == pytest.approx(
        server._TURN_END_DWELL_S + 0.5, abs=0.1
    )


def test_two_ticks_racing_announce_exactly_once(monkeypatch):
    """``claim_work`` is the permission to speak, and it is atomic.

    Two unsynchronised 4s tickers plus an on-demand ``_republish_session`` all
    run this gate. A plain read-then-clear lets two of them satisfy the dwell in
    the same instant — and the duplicate would be invisible on the phone (the
    per-channel dedupe eats it) and perfectly visible in the bell, whose rows
    are keyed by event seq.
    """
    import threading

    b = EventBus()
    monkeypatch.setattr(events_mod, "BUS", b)
    got = []
    b.subscribe(got.append)
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: False)
    monkeypatch.setattr(server._prompt_queue, "peek_next", lambda title: None)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    # The REAL claim, deliberately: this test is about that function's atomicity.
    agent_state._ACTIVITY_CACHE["race"] = {
        "created": 1.0,
        "reported": "idle",
        "state_since": 0.0,
        "worked_at": 5.0,
    }
    snap = {"activity": "idle", "activity_at": 0.0}
    ready = threading.Barrier(8)

    def _go():
        ready.wait()
        server._note_turn_boundary("race", snap, 10_000.0)

    threads = [threading.Thread(target=_go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_ended(got)) == 1
    agent_state._ACTIVITY_CACHE.pop("race", None)


def test_a_failing_probe_never_breaks_the_tick(bus, monkeypatch):
    """A notification is the least important thing in the loop it rides."""
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)

    def _boom(*a, **kw):
        raise RuntimeError("queue store is unreadable")

    monkeypatch.setattr(server._prompt_queue, "peek_next", _boom)
    _tick("s10", "working")
    _tick("s10", "idle")
    _tick("s10", "idle")  # the tick completes...
    assert _ended(got) == []  # ...and simply says nothing
    # The same for the emit itself: the bus is the last thing to go wrong here.
    monkeypatch.setattr(server._prompt_queue, "peek_next", lambda title: None)
    monkeypatch.setattr(bus, "emit", _boom)
    server._note_turn_boundary("s10", {"activity": "idle", "activity_at": 0.0}, 1e9)


# --------------------------------------------------------------------------- #
# The record's lifetime: what may forget it, and what may not
# --------------------------------------------------------------------------- #
_LIVE = "keep-me"
_GONE = "left-us"


@pytest.fixture()
def seeded_state():
    """Rolling state for two sessions, in every dict that carries any."""

    def _seed(title, worked=1000.0):
        server._ACTIVITY_CACHE[title] = {
            "created": 1.0,
            "reported": "idle",
            "state_since": 900.0,
            "worked_at": worked,
            "hash": "abc",
        }
        server._LIMIT_PROBE[title] = {"at": 1.0, "limit": True}
        server._TRUST_DISMISS_AT[title] = 1.0
        # This one alone is keyed by TMUX SESSION NAME, not by title.
        server._THREAD_RECORD_AT[server.tmux.to_mindflock_tmux_name(title)] = 1.0

    _seed(_LIVE)
    _seed(_GONE)
    yield _seed
    for title in (_LIVE, _GONE):
        for _d in (
            server._ACTIVITY_CACHE,
            server._LIMIT_PROBE,
            server._TRUST_DISMISS_AT,
        ):
            _d.pop(title, None)
        server._THREAD_RECORD_AT.pop(server.tmux.to_mindflock_tmux_name(title), None)


def test_pressing_commit_keeps_the_work_evidence(seeded_state):
    """THE REGRESSION THE SPLIT EXISTS TO PREVENT.

    Commit / Push / Make-PR / merge all call ``_forget_probes`` to drop a
    2.5s-old stage. When that also popped the rolling record, pressing Commit
    at the end of a run destroyed the very evidence the turn-end announcement
    is built on — so the sessions a user was most likely to be watching were
    exactly the ones that never told them they were done.
    """
    with server._PROBE_CACHE_LOCK:
        server._PROBE_CACHE[("stage", _LIVE)] = (0.0, {"stage": "agent"})
    server._forget_probes(_LIVE)
    # The memoized result is gone (that IS the point of the call)…
    with server._PROBE_CACHE_LOCK:
        assert ("stage", _LIVE) not in server._PROBE_CACHE
    # …and the session's history is untouched.
    assert server._agent_state.worked_at(_LIVE) == 1000.0
    assert server._agent_state.state_since(_LIVE) == 900.0
    assert _LIVE in server._LIMIT_PROBE


def test_deleting_a_session_forgets_it_entirely(seeded_state):
    """Titles are reused — ``untitled-2`` comes straight back — so a namesake
    must not inherit the dead session's pane hash, limit verdict or history."""
    name = server.tmux.to_mindflock_tmux_name(_GONE)
    assert name != _GONE, "the translation is the whole point of the last line"
    server._forget_session_state(_GONE)
    assert _GONE not in server._ACTIVITY_CACHE
    assert _GONE not in server._LIMIT_PROBE
    assert _GONE not in server._TRUST_DISMISS_AT
    assert name not in server._THREAD_RECORD_AT
    # Its neighbour is untouched.
    assert server._agent_state.worked_at(_LIVE) == 1000.0
    assert server.tmux.to_mindflock_tmux_name(_LIVE) in server._THREAD_RECORD_AT


def test_the_teardown_routes_forget_the_whole_record_not_just_the_probes():
    """Which of the two functions each caller gets is the entire fix, and it is
    invisible at runtime until a user presses the wrong button — so pin it."""
    import inspect

    for route in (
        server.delete_instance,
        server.instance_cleanup,
        server.close_instance,
    ):
        src = inspect.getsource(route)
        assert "_forget_session_state(title)" in src, route.__name__
    # And the workflow verbs must NOT: they only want fresh probes.
    assert "_forget_probes" in inspect.getsource(server._forget_session_state)


def test_prune_drops_departed_sessions_and_keeps_the_flock(seeded_state):
    server._prune_session_state([_LIVE])
    assert _GONE not in server._ACTIVITY_CACHE
    assert _GONE not in server._LIMIT_PROBE
    assert _GONE not in server._TRUST_DISMISS_AT
    assert server.tmux.to_mindflock_tmux_name(_GONE) not in server._THREAD_RECORD_AT
    assert server._agent_state.worked_at(_LIVE) == 1000.0
    assert server.tmux.to_mindflock_tmux_name(_LIVE) in server._THREAD_RECORD_AT


def test_the_tick_sweeps_a_session_that_left_without_a_route(monkeypatch, seeded_state):
    """A workspace deleted from Settings, or a tombstone converged from another
    MindFlock on the same state file, pops the instance without passing through
    a teardown route. Nothing else would ever drop its state."""
    monkeypatch.setattr(server, "_build_instances_snapshot", lambda: [])
    monkeypatch.setattr(server._events, "set_sessions_snapshot", lambda out: None)
    # Per the suite's rule about the REAL engine: replace the mapping, never
    # mutate the live one.
    monkeypatch.setattr(server.ENGINE, "instances", {_LIVE: object()})
    server._instances_tick()
    assert _GONE not in server._ACTIVITY_CACHE
    assert _LIVE in server._ACTIVITY_CACHE


def test_republishing_a_session_keeps_its_work_evidence(monkeypatch, seeded_state):
    """``_republish_session`` runs right after commit/push/PR — the same moments
    ``_forget_probes`` fires. It must not lose the record either."""
    monkeypatch.setattr(server.ENGINE, "instances", {_LIVE: object()})
    monkeypatch.setattr(
        server,
        "_session_snapshot",
        lambda inst, queues: {
            "status": "running",
            "activity": "idle",
            "stage": "agent",
        },
    )
    monkeypatch.setattr(server._events, "patch_session_snapshot", lambda t, d: None)
    assert server._republish_session(_LIVE) is not None
    assert server._agent_state.worked_at(_LIVE) == 1000.0
