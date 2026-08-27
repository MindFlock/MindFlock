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

import time
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
    agent_state._verdict(rec, "working", 1010.0, arms=True)
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
    agent_state._verdict(rec, "working", 1000.0, arms=True)
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
    agent_state._verdict(rec, "working", 1000.0, arms=True)
    assert "s" not in agent_state._ACTIVITY_CACHE
    assert agent_state.worked_at("s") is None


def test_limit_spends_the_work_evidence():
    # A turn the usage cap cut short is not a turn that finished. The reading
    # only says "limit" while the banner is on the pane, so without dropping the
    # evidence here the session announces "has finished" the moment the pane
    # redraws — for work that was abandoned mid-thought.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0, arms=True)
    assert agent_state.worked_at("s") == 1000.0
    agent_state._verdict(rec, "limit", 1010.0)
    assert agent_state.worked_at("s") is None
    agent_state._verdict(rec, "idle", 1020.0)
    assert agent_state.worked_at("s") is None, "a redrawn prompt must not re-arm it"
    # A resume re-earns it, so the genuine ending still announces.
    agent_state._verdict(rec, "working", 1030.0, arms=True)
    assert agent_state.worked_at("s") == 1030.0


def test_claim_work_is_once_only():
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0, arms=True)
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
    agent_state._verdict(rec, "working", 1000.0, arms=True)

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
# Which readings may ARM a turn end
#
# A reading is enough to paint the chip. It is not, by itself, enough to
# interrupt a human. These pin the ladder: the CLI's own word and its live-turn
# status line arm; a busy process tree does not, unless the CPU rate is
# genuinely all the CLI has to offer.
# --------------------------------------------------------------------------- #
class _StubProvider:
    """One configurable CLI: does it report for itself, does it declare a
    live-turn pane pattern, and what does its marker say."""

    def __init__(self, *, reports=False, patterns=(), marker=None, age=None):
        self._reports, self._patterns = reports, patterns
        self._marker, self._age = marker, age

    def reports_activity(self):
        return self._reports

    def activity_state(self, name):
        return self._marker

    def activity_state_age(self, name):
        return self._age

    def record_thread(self, *a, **k):
        return None

    def waiting_prompt_patterns(self):
        return []

    def working_pane_patterns(self):
        return self._patterns

    def progress_token_pattern(self):
        return None


_QUIET_PANE = "line-1\nline-2\nline-3\nfiller\nbottom-a\nbottom-b\n"
_LIVE_TURN_PANE = "\u283b Thinking… (esc to interrupt)\n" + _QUIET_PANE


@pytest.fixture()
def pane(monkeypatch):
    """A controllable pane: text, CPU jiffies and a fake clock, so a poll is a
    function call rather than a wait. Mirrors the harness in
    test_activity_resize.py; kept local because these tests drive the CPU
    counter, which that one's fixture does not expose per-poll."""
    state = {"text": _QUIET_PANE, "cpu": 0, "t": 1000.0, "provider": _StubProvider()}
    monkeypatch.setattr(server.time, "time", lambda: state["t"])
    monkeypatch.setattr(server.tmux, "to_mindflock_tmux_name", lambda t: t)
    monkeypatch.setattr(server, "_agent_exited", lambda name, created: False)
    monkeypatch.setattr(server, "_pane_has_agent_process", lambda pid: True)
    monkeypatch.setattr(server.providers, "resolve", lambda prog: state["provider"])
    monkeypatch.setattr(
        server, "_pane_meta", lambda name: ("node", 1.0, "123", "80x24")
    )
    monkeypatch.setattr(server, "_pane_cpu_jiffies", lambda pid: state["cpu"])
    monkeypatch.setattr(server, "_dismiss_trust_prompt", lambda *a, **k: False)
    monkeypatch.setattr(server.session, "Paused", "paused", raising=False)

    def fake_run(argv, **kw):
        joined = " ".join(argv)
        if "has-session" in joined:
            return types.SimpleNamespace(returncode=0, stdout=b"")
        if "capture-pane" in joined:
            return types.SimpleNamespace(returncode=0, stdout=state["text"].encode())
        return types.SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    return state


def _poll(state, *, jiffies=0):
    """One 4s tick, having burned ``jiffies`` of CPU since the last one.
    120 jiffies over 4s is 30/s — above `_CPU_ACTIVE_JIFFIES_PER_S` (25)."""
    state["t"] += 4.0
    state["cpu"] += jiffies
    inst = types.SimpleNamespace(
        Title="s", Program="claude", Status="running", Started=lambda: True
    )
    return server._agent_activity(inst, "s")


def test_an_unarmed_working_reading_moves_the_badge_and_arms_nothing():
    # The split the whole fix rests on, at its smallest.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0)
    assert agent_state.state_since("s") == 1000.0, "the chip still turns green"
    assert agent_state.worked_at("s") is None, "and nothing may be announced"


def test_a_cpu_spike_on_a_reporting_cli_arms_nothing(pane):
    """THE PHANTOM-FINISH FIX.

    A parked Claude session, untouched for 19 hours, announced "has finished".
    Its own auto-updater crossed `_CPU_ACTIVE_JIFFIES_PER_S` for a single poll;
    that read as working for ~12s — past the announce path's flicker settle —
    and 45s of quiet later the turn-end gate found work evidence waiting.

    The CLI reports for itself and its hooks said nothing, and no interrupt hint
    was ever on the pane. Whatever burned that CPU, it was not a turn.
    """
    # A STALE working marker: the hooks demonstrably speak (the marker
    # machinery works), classification just fell through to the pane. Stale
    # past the 45s trust window but young enough to belong to this
    # incarnation — a marker older than the 6h freshness cut reads as None
    # and (rightly) re-earns the backstop, being indistinguishable from hooks
    # that never worked.
    pane["provider"] = _StubProvider(
        reports=True, patterns=(r"esc to interrupt",), marker="working", age=60.0
    )
    assert _poll(pane) == "idle"
    # The spike: real CPU, a redrawn pane, and no sign of a live turn on it.
    pane["text"] = "a new line appeared\n" + _QUIET_PANE
    assert _poll(pane, jiffies=120) == "working"
    assert agent_state.worked_at("s") is None
    # Not after a while, either: a CLI whose hooks are actually SPEAKING gets
    # no CPU backstop, so an hour of a busy process tree is still not a turn.
    started = pane["t"]
    while pane["t"] - started < agent_state._CPU_ONLY_ARMS_AFTER_S * 3:
        assert _poll(pane, jiffies=120) == "working"
    assert agent_state.worked_at("s") is None
    # And it decays back to idle on its own, still having armed nothing.
    for _ in range(4):
        _poll(pane)
    assert agent_state.worked_at("s") is None


def test_a_declared_reporter_whose_hooks_never_speak_keeps_the_backstop(pane):
    # reports_activity is a config DECLARATION: a codex build without the
    # hooks engine, or a hook install that failed silently, still declares
    # True while activity_state answers None forever. Denying that session
    # the backstop leaves one regex as its only route to a turn-end — so a
    # silent marker keeps the sustained-CPU rung, spikes still excluded.
    pane["provider"] = _StubProvider(reports=True, patterns=(), marker=None)
    assert _poll(pane) == "idle"
    assert _poll(pane, jiffies=120) == "working"
    assert agent_state.worked_at("s") is None, "one busy poll is still a spike"
    started = pane["t"]
    while pane["t"] - started < agent_state._CPU_ONLY_ARMS_AFTER_S:
        assert _poll(pane, jiffies=120) == "working"
    assert agent_state.worked_at("s") == pane["t"]


def test_the_live_turn_status_line_arms_even_with_no_marker(pane):
    # The other half: an interrupt hint is on the pane BECAUSE a turn is
    # running. It is turn-specific proof, so it arms whether or not the CLI's
    # own hooks are reporting — which is the case where a marker went stale
    # mid-turn and the pane is all that is left.
    pane["provider"] = _StubProvider(reports=True, patterns=(r"esc to interrupt",))
    pane["text"] = _LIVE_TURN_PANE
    assert _poll(pane) == "working"
    assert agent_state.worked_at("s") == pane["t"]


def test_a_fresh_working_marker_arms_at_any_duration(pane):
    # Layer 1. A hook fires because a prompt was submitted or a tool ran; there
    # is no cosmetic redraw behind it, so no dwell is required of it.
    pane["provider"] = _StubProvider(reports=True, marker="working", age=0.0)
    assert _poll(pane) == "working"
    assert agent_state.worked_at("s") == pane["t"]


def test_a_cli_that_cannot_report_keeps_a_cpu_backstop(pane):
    # Five of the six shipped providers have no hooks: their whole turn signal
    # is one regex against a status line, and a regex can be wrong (the CLI
    # reworded its hint, nobody filled one in). Losing the announcement to a bad
    # pattern is worse than the spike the strict rule protects against, so a
    # SUSTAINED busy run still arms here — `_CPU_ONLY_ARMS_AFTER_S`, which is
    # longer than any single spike survives. A CLI that does report gets no such
    # backstop; it has two independent signals already.
    pane["provider"] = _StubProvider(reports=False, patterns=(r"esc to interrupt",))
    assert _poll(pane) == "idle"
    assert _poll(pane, jiffies=120) == "working"
    assert agent_state.worked_at("s") is None, "one busy poll is still a spike"
    started = pane["t"]
    while pane["t"] - started < agent_state._CPU_ONLY_ARMS_AFTER_S:
        assert _poll(pane, jiffies=120) == "working"
    assert agent_state.worked_at("s") == pane["t"]


def test_two_spikes_either_side_of_a_lull_do_not_add_up(pane):
    # The run has to be unbroken: settling back to idle drops the clock, so a
    # session that twitches every few minutes never accumulates a turn.
    pane["provider"] = _StubProvider(reports=False, patterns=())
    assert _poll(pane) == "idle"
    for _ in range(2):
        assert _poll(pane, jiffies=120) == "working"
        for _ in range(4):  # CPU quiet, past _ACTIVITY_IDLE_AFTER
            _poll(pane)
        assert agent_state._ACTIVITY_CACHE["s"].get("hard_since") is None
    assert agent_state.worked_at("s") is None


# --------------------------------------------------------------------------- #
# Reading sources: which layer spoke, and who gets to skip the guard rails
#
# _verdict records WHERE each reading came from; reading_is_authoritative is
# the one question two consumers ask (the announce path's settle skip, the
# queue drain's fast tier), and work_evidence is what the dwell tiers key on.
# --------------------------------------------------------------------------- #
def test_verdict_records_the_source_with_the_value():
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0, arms=True, source="marker")
    assert rec["source"] == "marker"
    agent_state._verdict(rec, "idle", 1010.0, source="pane")
    assert rec["source"] == "pane"


def test_authoritative_needs_both_the_source_AND_the_value():
    # The value comparison is the memo-skew guard: the tickers serve activity
    # out of a 2.5s memo while the drain probes uncached, so the record can
    # describe a NEWER reading than the value a caller holds. A mismatch must
    # answer False — never let a stale value borrow a fresh reading's
    # authority.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "idle", 1000.0, source="marker")
    assert agent_state.reading_is_authoritative("s", "idle")
    assert not agent_state.reading_is_authoritative("s", "working")
    agent_state._verdict(rec, "idle", 1010.0, source="pane")
    assert not agent_state.reading_is_authoritative("s", "idle")
    assert not agent_state.reading_is_authoritative("never-seen", "idle")


def test_exit_marker_is_authoritative_and_process_tree_is_not():
    # An exit marker is written once, at process death — definitive. The
    # process-tree probe can misread transiently (a ps race), which is exactly
    # what the settle exists to absorb, so it stays on the guarded path.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "idle", 1000.0, source="exit")
    assert agent_state.reading_is_authoritative("s", "idle")
    agent_state._verdict(rec, "idle", 1010.0, source="proc")
    assert not agent_state.reading_is_authoritative("s", "idle")


def test_work_evidence_labels_the_armed_tier_and_survives_the_claim():
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0, arms=True, source="marker")
    assert agent_state.work_evidence("s") == "marker"
    # Spending the evidence keeps the label: it describes the cycle that was
    # just claimed, and it is only ever read next to worked_at.
    assert agent_state.claim_work("s") == 1000.0
    assert agent_state.work_evidence("s") == "marker"


def test_pane_armed_evidence_carries_the_pane_proof():
    rec = agent_state._activity_record("s", 100.0)
    rec["proof"] = "status"
    agent_state._verdict(rec, "working", 1000.0, arms=True, source="pane")
    assert agent_state.work_evidence("s") == "status"
    rec["proof"] = "cpu"
    agent_state._verdict(rec, "working", 1010.0, arms=True, source="pane")
    assert agent_state.work_evidence("s") == "cpu"


def test_marker_working_evidence_is_marker_even_with_stale_pane_proof():
    # A pane run earlier in the session leaves proof="status" behind; the
    # CLI's own report must not inherit a weaker (or merely different) label.
    rec = agent_state._activity_record("s", 100.0)
    rec["proof"] = "cpu"
    agent_state._verdict(rec, "working", 1000.0, arms=True, source="marker")
    assert agent_state.work_evidence("s") == "marker"


# --------------------------------------------------------------------------- #
# The forced limit probe: the first idle after a turn always captures fresh
# --------------------------------------------------------------------------- #
class _Srv:
    """Stand-in for the server module: _idle_or_limit only needs _run_capped."""

    def __init__(self, pane_text):
        self.pane_text = pane_text
        self.captures = 0

    def _run_capped(self, cmd, **kw):
        self.captures += 1
        return types.SimpleNamespace(
            returncode=0, stdout=self.pane_text.encode("utf-8")
        )


def test_first_idle_after_a_turn_probes_the_pane_fresh(monkeypatch):
    """THE LIMIT BLIND-WINDOW FIX.

    A turn shorter than the probe throttle that ends BECAUSE of the limit used
    to be invisible: the cache held a pre-turn "no banner" verdict, and if the
    banner left the pane before the throttle expired, the cut-short turn's
    work evidence stayed armed forever — a "has finished" for abandoned work.
    Forcing a fresh capture on the working→idle transition closes the window
    at the moment it opens.
    """
    monkeypatch.setattr(agent_state.time, "time", lambda: 1000.0)
    # A fresh-looking cached verdict from BEFORE the turn: no banner.
    agent_state._LIMIT_PROBE["s"] = {"at": 999.0, "limit": False}
    srv = _Srv("Claude usage limit reached · resets 3am\n❯ 1. Wait\n")
    out = agent_state._idle_or_limit(srv, "s", "s", None, force=True)
    assert out == "limit"
    assert srv.captures == 1, "the throttle was bypassed"


def test_steady_state_idle_keeps_the_throttle(monkeypatch):
    monkeypatch.setattr(agent_state.time, "time", lambda: 1000.0)
    agent_state._LIMIT_PROBE["s"] = {"at": 999.0, "limit": False}
    srv = _Srv("Claude usage limit reached\n")
    out = agent_state._idle_or_limit(srv, "s", "s", None)
    assert out == "idle", "cached verdict served — no banner known yet"
    assert srv.captures == 0, "one capture per _LIMIT_RECHECK_S, as before"


def test_a_forced_miss_does_not_buy_the_throttle_a_fresh_window(monkeypatch):
    # The one forced capture can race the CLI's banner paint or fail outright
    # (a busy tmux server). A miss must leave the throttle OPEN so the next
    # 4s poll re-probes — caching it would serve "no banner" for 15s while
    # the fast dwell announces a limit-cut turn as finished.
    monkeypatch.setattr(agent_state.time, "time", lambda: 1000.0)
    agent_state._LIMIT_PROBE.pop("s", None)
    srv = _Srv("a plain prompt, banner not painted yet\n")
    assert agent_state._idle_or_limit(srv, "s", "s", None, force=True) == "idle"
    # A fresh cache seeded 0.5s ago (the pre-turn steady-state probe):
    agent_state._LIMIT_PROBE["s"] = {"at": 999.5, "limit": False}
    assert agent_state._idle_or_limit(srv, "s", "s", None, force=True) == "idle"
    entry = agent_state._LIMIT_PROBE.get("s") or {}
    assert float(entry.get("at", 1.0)) == 0.0, (
        "a forced miss zeroes the stamp so the NEXT poll re-probes at tick "
        "cadence instead of trusting a possibly pre-banner frame for 15s"
    )
    # And the banner painting late is caught by that very next poll.
    srv2 = _Srv("Claude usage limit reached · resets 3am\n")
    assert agent_state._idle_or_limit(srv2, "s", "s", None) == "limit"


def test_marker_idle_after_working_forces_the_probe(pane):
    # End to end through _agent_activity: the reading before the Stop was
    # "working" (marker), the Stop lands with a limit banner on the pane and a
    # FRESH (1s-old) cached no-banner verdict — the transition must see the
    # banner anyway, and the cut-short turn's evidence must be dropped.
    pane["provider"] = _StubProvider(reports=True, marker="working", age=0.0)
    assert _poll(pane) == "working"
    assert agent_state.worked_at("s") is not None
    # Cache stamped with REAL wall time (agent_state keeps its own clock; the
    # fixture only fakes the server module's): the throttle would serve this
    # no-banner verdict for 15 more real seconds, so only the forced fresh
    # capture can explain a "limit" answer below.
    import time as _real_time

    agent_state._LIMIT_PROBE["s"] = {"at": _real_time.time(), "limit": False}
    pane["provider"] = _StubProvider(reports=True, marker="idle", age=0.0)
    pane["text"] = "Claude usage limit reached · resets 3am\n" + _QUIET_PANE
    assert _poll(pane) == "limit"
    assert agent_state.worked_at("s") is None, "a cut-short turn is not a turn"


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


def test_dwell_tier_ordering_and_exact_gate_coverage():
    """The ordering these numbers have to keep, pinned so a future tweak of any
    one of them cannot silently invert it.

    The tiers must be ordered by evidence strength — the weaker the evidence,
    the longer the dwell — and the hazards the old flat 45s blanketed are now
    covered by exact gates, each of which has its own constant to keep honest:
    the send-grace must outlast the drain's whole worst-case pickup path, and
    the human hold must exceed the inter-turn gap of a person reading a long
    answer and typing a follow-up (10-30s).
    """
    from backend.web.core import autopilot

    # Weaker evidence -> longer dwell.
    assert (
        server._TURN_END_DWELL_MARKER_S
        < server._TURN_END_DWELL_STATUS_S
        < server._TURN_END_DWELL_S
    )
    # Every tier must be large against the 4s tick (two unsynchronised tickers
    # plus an on-demand republish shift the answer by about a tick).
    assert server._TURN_END_DWELL_MARKER_S >= 12.0
    # The slow tier keeps the old blanket relations: cpu-armed evidence gets
    # no exact-gate credit, so it still outlasts the queue's and autopilot's
    # own settles the way the flat dwell always did.
    assert server._TURN_END_DWELL_S > server._QUEUE_IDLE_SETTLE
    assert server._TURN_END_DWELL_S > autopilot.IDLE_SETTLE_S
    # The status tier must exceed the pane layer's own idle hysteresis plus
    # the settle — its turn-END detection is itself pane-derived.
    assert server._TURN_END_DWELL_STATUS_S > (
        server._agent_state._ACTIVITY_IDLE_AFTER + server._ACTIVITY_SETTLE_SECONDS
    )
    # The send-grace covers the pop->visible-working window: a reboot's boot
    # grace plus the drain's own settle plus a couple of passes.
    assert server._QUEUE_SEND_GRACE_S > (
        server._QUEUE_BOOT_GRACE + server._QUEUE_IDLE_SETTLE + 10.0
    )
    # A person reading a long answer types the follow-up within 10-30s.
    assert server._HUMAN_HOLD_S >= 30.0
    # The drain's fast tier still spans the ~1s Stop->UserPromptSubmit lull
    # with margin, and stays under the guarded tier.
    assert 2.0 < server._QUEUE_IDLE_SETTLE_MARKER < server._QUEUE_IDLE_SETTLE
    # And every tier must dwarf the flicker filter, which answers a different
    # question ("did two probes agree?") and cannot answer this one.
    assert server._TURN_END_DWELL_MARKER_S > server._ACTIVITY_SETTLE_SECONDS * 3


def test_marker_evidence_announces_on_the_fast_tier(bus, monkeypatch):
    """The whole point of the tiering: a hook CLI's finished run is announced
    in ~12s instead of ~45, because every hazard the extra 33s used to blanket
    is checked exactly."""
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    monkeypatch.setattr(server._agent_state, "work_evidence", lambda t: "marker")
    # The fast lane also requires the END to be the CLI's own word (see the
    # mismatch test below) — grant it here.
    monkeypatch.setattr(
        server._agent_state, "reading_is_authoritative", lambda t, v: True
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    _tick("s20", "working")
    clock["t"] += 4
    _tick("s20", "idle")
    clock["t"] += 4
    _tick("s20", "idle")  # settles the chip
    # Under the fast dwell but past what the settle alone would allow.
    clock["t"] += server._TURN_END_DWELL_MARKER_S - 6
    _tick("s20", "idle")
    assert _ended(got) == []
    clock["t"] += 8
    _tick("s20", "idle")
    assert len(_ended(got)) == 1
    assert _ended(got)[0]["data"]["idle_for"] < server._TURN_END_DWELL_S


def test_cpu_evidence_stays_on_the_slow_tier(bus, monkeypatch):
    # The backstop tier bought its admission with the 45s dwell; a fast
    # announcement on CPU-armed evidence would resurrect the phantom-finish.
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    monkeypatch.setattr(server._agent_state, "work_evidence", lambda t: "cpu")
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    _tick("s21", "working")
    clock["t"] += 4
    _tick("s21", "idle")
    clock["t"] += 4
    _tick("s21", "idle")
    clock["t"] += server._TURN_END_DWELL_STATUS_S + 5
    _tick("s21", "idle")
    assert _ended(got) == [], "status-tier time is not enough for cpu evidence"
    clock["t"] += server._TURN_END_DWELL_S
    _tick("s21", "idle")
    assert len(_ended(got)) == 1


def test_marker_armed_work_with_a_pane_detected_end_takes_the_slow_lane(
    bus, monkeypatch
):
    # The tier is keyed on how the WORK was corroborated, but the END must be
    # the CLI's own word to ride the fast lane: a marker-armed turn whose idle
    # came from the pane (the marker went stale mid-turn, a scrolled-back
    # capture read as parked) is a pane-detected end wearing marker evidence,
    # and 12s of that would announce — and SPEND — a turn still running.
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    monkeypatch.setattr(server._agent_state, "work_evidence", lambda t: "marker")
    monkeypatch.setattr(
        server._agent_state, "reading_is_authoritative", lambda t, v: False
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    _tick("s29", "working")
    clock["t"] += 4
    _tick("s29", "idle")
    clock["t"] += 4
    _tick("s29", "idle")
    clock["t"] += server._TURN_END_DWELL_MARKER_S + 8
    _tick("s29", "idle")
    assert _ended(got) == [], "a pane-detected end must not ride the fast lane"
    clock["t"] += server._TURN_END_DWELL_S
    _tick("s29", "idle")
    assert len(_ended(got)) == 1


def test_recent_human_input_holds_the_announcement(bus, monkeypatch):
    # A person mid-follow-up is not a finished run — and a person typing in
    # the window does not need a push about the window.
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    server._note_human_input("s22")
    try:
        for _ in range(3):
            _tick("s22", "idle")
        assert _ended(got) == []
        # The person walked away: the hold expires and the announcement lands.
        server._HUMAN_INPUT_AT["s22"] -= server._HUMAN_HOLD_S + 1
        _tick("s22", "idle")
        assert len(_ended(got)) == 1
    finally:
        server._HUMAN_INPUT_AT.pop("s22", None)


def test_a_sent_but_unstarted_prompt_holds_the_announcement(bus, monkeypatch):
    """THE POPPED-PROMPT WINDOW.

    record_sent pops the final queued item the instant tmux typing succeeds,
    so peek_next reads None seconds before the agent visibly starts (or
    never, for a send that didn't take). The drain's own record carries the
    truth: disarmed + recently sent = in flight.
    """
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    server._QUEUE_STATE["s23"] = {
        "armed": False,
        "sent_at": time.time(),
        "rebooted_at": 0.0,
        "idle_since": None,
    }
    try:
        for _ in range(3):
            _tick("s23", "idle")
        assert _ended(got) == []
        # The drain observed the agent working since: the flight is over, and
        # a later idle may announce.
        server._QUEUE_STATE["s23"]["armed"] = True
        _tick("s23", "idle")
        assert len(_ended(got)) == 1
    finally:
        server._QUEUE_STATE.pop("s23", None)


def test_a_lost_send_does_not_hold_forever(bus, monkeypatch):
    # Past the grace with the agent never seen working, the send is presumed
    # lost and the announcement falls through — the old flat dwell's behavior.
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    server._QUEUE_STATE["s24"] = {
        "armed": False,
        "sent_at": time.time() - server._QUEUE_SEND_GRACE_S - 1,
        "rebooted_at": 0.0,
        "idle_since": None,
    }
    try:
        _tick("s24", "working")
        _tick("s24", "idle")
        _tick("s24", "idle")
        assert len(_ended(got)) == 1
    finally:
        server._QUEUE_STATE.pop("s24", None)


def test_a_running_fast_track_chain_holds_the_announcement(bus, monkeypatch):
    # "The chain decided the agent was done, THEN we said so" — now enforced
    # exactly rather than by dwell arithmetic, so it holds at any tier.
    from backend.web.core import autopilot as ap

    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    ap.arm("s25", "push")
    # A live driver holds the lease (claim refreshes owner_at every ~5s pass);
    # the gate is bounded by exactly that lease, so grant it here.
    ap.claim("s25", "test-driver")
    try:
        for _ in range(3):
            _tick("s25", "idle")
        assert _ended(got) == []
        ap.finish("s25")
        _tick("s25", "idle")
        assert len(_ended(got)) == 1
    finally:
        ap.disarm("s25")


def test_a_wedged_running_chain_cannot_mute_announcements_forever(bus, monkeypatch):
    # _autopilot_observe early-returns WITHOUT claiming on the paths it cannot
    # step (budget lock, missing worktree), so a "running" record nobody is
    # advancing goes lease-stale — and announcements must resume rather than
    # be muted for as long as the record says "running".
    from backend.web.core import autopilot as ap

    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    ap.arm("s30", "push")
    ap.claim("s30", "test-driver")
    ap.update("s30", owner_at=time.time() - ap.LEASE_STALE_S * 2 - 1)
    try:
        _tick("s30", "working")
        _tick("s30", "idle")
        _tick("s30", "idle")
        assert len(_ended(got)) == 1, "a lease-stale running chain must not hold"
    finally:
        ap.disarm("s30")


# --------------------------------------------------------------------------- #
# The settle skip: an authoritative reading adopts in ONE tick
# --------------------------------------------------------------------------- #
def test_marker_sourced_idle_skips_the_settle(bus, monkeypatch):
    """A hook marker cannot flicker — it changes only when the CLI fires a
    hook — so parking it 3s+a tick was pure latency on the chip and on the
    default-on needs-input/limit pushes."""
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: None)
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 3.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    server._ACTIVITY_CACHE["s26"] = {
        "created": 1.0,
        "reported": "idle",
        "state_since": 0.0,
        "worked_at": None,
        "reading": ("idle", "marker"),
    }
    try:
        _tick("s26", "working")
        clock["t"] += 4
        _tick("s26", "idle")  # ONE sighting — adopted immediately
        idles = [
            e
            for e in got
            if e["event"] == "session.activity_changed" and e["new"] == "idle"
        ]
        assert len(idles) == 1, "authoritative idle must not park"
        assert server._EVENT_SNAPSHOT["s26"]["activity"] == "idle"
    finally:
        server._ACTIVITY_CACHE.pop("s26", None)
        server._EVENT_SNAPSHOT.pop("s26", None)


def test_pane_sourced_idle_still_settles(bus, monkeypatch):
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: None)
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 3.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    server._ACTIVITY_CACHE["s27"] = {
        "created": 1.0,
        "reported": "idle",
        "state_since": 0.0,
        "worked_at": None,
        "reading": ("idle", "pane"),
    }
    try:
        _tick("s27", "working")
        clock["t"] += 4
        _tick("s27", "idle")  # parked
        assert server._EVENT_SNAPSHOT["s27"]["activity"] == "working"
        clock["t"] += 4
        _tick("s27", "idle")  # settled
        assert server._EVENT_SNAPSHOT["s27"]["activity"] == "idle"
    finally:
        server._ACTIVITY_CACHE.pop("s27", None)
        server._EVENT_SNAPSHOT.pop("s27", None)


def test_a_chained_turn_boundary_is_not_swallowed(bus, monkeypatch):
    # Stop then UserPromptSubmit inside one drain pass (a queue picking up the
    # next prompt): the authoritative idle adopts in ONE sighting and the
    # working that follows immediately must also emit — neither leg of the
    # boundary may vanish into a settle window.
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: None)
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 3.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    server._ACTIVITY_CACHE["s33"] = {
        "created": 1.0,
        "reported": "idle",
        "state_since": 0.0,
        "worked_at": None,
        "reading": ("idle", "marker"),
    }
    try:
        _tick("s33", "working")
        clock["t"] += 4
        _tick("s33", "idle")  # Stop hook — adopted on one sighting
        server._ACTIVITY_CACHE["s33"]["reading"] = ("working", "marker")
        _tick("s33", "working")  # UserPromptSubmit, the very next tick
        acts = [e["new"] for e in got if e["event"] == "session.activity_changed"]
        assert "idle" in acts, "the Stop leg emitted"
        assert acts and acts[-1] == "working", "the restart leg emitted after it"
        assert server._EVENT_SNAPSHOT["s33"]["activity"] == "working"
    finally:
        server._ACTIVITY_CACHE.pop("s33", None)
        server._EVENT_SNAPSHOT.pop("s33", None)


def test_marker_clarify_skips_the_settle_and_limit_never_does(bus, monkeypatch):
    """The two settle-skip cases that feed DEFAULT-ON pushes.

    A marker clarify (Notification/PermissionRequest hook) is the CLI's own
    word — the "needs your input" push may fire on one sighting. A "limit"
    reading never skips, whatever its tag: both marker-branch limit
    reclassifications are really pane captures matched against the limit
    patterns, and a single frame CAN misread those (copy-mode scrollback,
    agent output quoting a banner) — the "ran out of usage" push keeps its
    two-sighting guard.
    """
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: None)
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 3.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    server._ACTIVITY_CACHE["s31"] = {
        "created": 1.0,
        "reported": "clarify",
        "state_since": 0.0,
        "worked_at": None,
        "reading": ("clarify", "marker"),
    }
    server._ACTIVITY_CACHE["s32"] = {
        "created": 1.0,
        "reported": "limit",
        "state_since": 0.0,
        "worked_at": None,
        "reading": ("limit", "marker"),
    }
    try:
        _tick("s31", "working")
        _tick("s32", "working")
        clock["t"] += 4
        _tick("s31", "clarify")
        _tick("s32", "limit")
        assert (
            server._EVENT_SNAPSHOT["s31"]["activity"] == "clarify"
        ), "the CLI's own needs-input is adopted in one sighting"
        assert (
            server._EVENT_SNAPSHOT["s32"]["activity"] == "working"
        ), "limit is a pane fact whatever its tag — it keeps the settle"
    finally:
        for t in ("s31", "s32"):
            server._ACTIVITY_CACHE.pop(t, None)
            server._EVENT_SNAPSHOT.pop(t, None)


def test_an_incarnation_reset_drops_reading_authority():
    # A relaunched tmux session must not inherit the dead run's authority any
    # more than its work evidence: the reset wipes the (value, source) pair,
    # and until the fresh incarnation's first _verdict there is nothing to
    # skip a settle on.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "idle", 1000.0, source="marker")
    assert agent_state.reading_is_authoritative("s", "idle")
    agent_state._activity_record("s", 200.0)  # new tmux incarnation
    assert not agent_state.reading_is_authoritative("s", "idle")


def test_a_stale_memo_cannot_borrow_a_fresh_readings_authority(bus, monkeypatch):
    # The record says the CURRENT reading is "working" (a fresh uncached drain
    # probe); the ticker arrives with a memoized "idle". The mismatch must
    # take the guarded path — value and source travel together or not at all.
    got = []
    bus.subscribe(got.append)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: None)
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 3.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    server._ACTIVITY_CACHE["s28"] = {
        "created": 1.0,
        "reported": "working",
        "state_since": 0.0,
        "worked_at": None,
        "reading": ("working", "marker"),
    }
    try:
        _tick("s28", "working")
        clock["t"] += 4
        _tick("s28", "idle")
        assert (
            server._EVENT_SNAPSHOT["s28"]["activity"] == "working"
        ), "a memoized idle with mismatched provenance must park"
    finally:
        server._ACTIVITY_CACHE.pop("s28", None)
        server._EVENT_SNAPSHOT.pop("s28", None)


def test_clarify_after_work_keeps_the_EARLIER_stamp():
    # `clarify` is not work, and it is not "no work either" — it must leave the
    # evidence exactly as it found it. Refreshing it here would restart the
    # dwell every time a permission prompt redrew, and clearing it would lose a
    # genuine turn to a mid-turn confirmation.
    rec = agent_state._activity_record("s", 100.0)
    agent_state._verdict(rec, "working", 1000.0, arms=True)
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
    agent_state._verdict(rec, "working", 1000.0, arms=True)
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
