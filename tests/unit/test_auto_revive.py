"""Auto-revive (``backend.web.core.revive``): an agent the kernel killed comes
back by itself once memory has recovered.

Runs against a fake server namespace — fake instances, fake exit markers, a
fake ``_ensure_agent_session`` — so nothing here touches tmux or launches an
agent. Pins:

* who counts: only an agent this process saw live, dying to SIGKILL (exit
  marker 137, or no marker and its hooks last said ``working``) — never a
  person's quit, never a session that was already Offline at boot, a paused
  one, or with ``general.revive_on_start`` off;
* the memory gate (held while low, without spending the agent's budget);
* one at a time, :data:`GAP_SECONDS` apart;
* the rate limit: :data:`AUTO_MAX_PER_WINDOW` an hour, then left Offline with
  the reason on its row;
* the skip rules re-checked at launch time;
* the diff emitter does not announce the trip through Offline while it is
  coming back (no bell / toast / push), and does once it is given up on.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from backend.config import settings as S
from backend.session.storage import Status
from backend.web.core import revive


class _Inst:
    def __init__(self, wt, *, status=Status.Running, program="claude"):
        self.Status = status
        self.PeerShare = ""
        self.InPlace = False
        self.Program = program
        self._wt = wt

    def Started(self):
        return True

    def GetWorktreePath(self):
        return self._wt


class _Srv(SimpleNamespace):
    """The slice of ``backend.web.server`` the auto-revive uses."""

    def __init__(self, instances, *, markers=None, fail=None):
        super().__init__()
        self.ENGINE = SimpleNamespace(instances=instances, lock=threading.RLock())
        self.markers = dict(markers or {})  # title -> exit code
        self.alive = set()
        self.launched = []
        self.republished = []
        self._fail = fail or {}

    def _read_exit_marker(self, name):
        for title, code in self.markers.items():
            if name.endswith(title):
                return code
        return None

    def _is_natural_exit(self, code):
        return code in (0, 130)

    def _live_session_name(self, name):
        return name if any(name.endswith(t) for t in self.alive) else None

    def _session_limited_until(self, title):
        return 0.0

    def _live_limit_reset(self, provider, now, inst=None):
        return None

    def _ensure_agent_session(self, inst, title):
        self.launched.append(title)
        err = self._fail.get(title)
        if err is None:
            self.alive.add(title)
        return "mindflock_" + title, err

    def _republish_session(self, title):
        self.republished.append(title)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    revive._auto_reset_for_tests()
    monkeypatch.setattr(revive, "AUTO_ENABLED", True)
    monkeypatch.setattr(revive.memory_pressure, "is_low", lambda **kw: False)
    yield
    revive._auto_reset_for_tests()


@pytest.fixture
def wt(tmp_path):
    d = tmp_path / "wt"
    d.mkdir()
    return str(d)


def _sync(fn, *args):
    """``auto_tick``'s ``start``: run the launch inline instead of a thread."""
    fn(*args)


def _kill(srv, title, code=137, now=None):
    """The agent was seen live, then the tick reads it Offline with ``code``."""
    inst = srv.ENGINE.instances[title]
    revive.observe(srv, title, inst, "working", now=now)
    srv.alive.discard(title)
    srv.markers[title] = code
    revive.observe(srv, title, inst, "offline", now=now)


# --------------------------------------------------------------------------- #
# who counts as killed
# --------------------------------------------------------------------------- #
def test_a_live_agent_killed_by_sigkill_is_queued_and_relaunched(wt):
    srv = _Srv({"a": _Inst(wt)})
    _kill(srv, "a")
    assert revive.holds_offline("a")
    assert revive.auto_state("a")["state"] == revive.AUTO_PENDING
    assert revive.auto_tick(srv, start=_sync) == "a"
    assert srv.launched == ["a"]
    assert srv.republished == ["a"]
    assert revive.auto_state("a") is None
    # Just revived: a stale Offline from the probe memo is not announced.
    assert revive.holds_offline("a")
    assert not revive.holds_offline("a", now=time.time() + 60)


def test_an_agent_offline_since_boot_is_not_auto_revived(wt):
    """Never seen live by this process: that is the startup sweep's call
    (and ``revive_on_start``'s), not a death to react to."""
    srv = _Srv({"a": _Inst(wt)}, markers={"a": 137})
    revive.observe(srv, "a", srv.ENGINE.instances["a"], "offline")
    assert not revive.holds_offline("a")
    assert revive.auto_tick(srv, start=_sync) is None
    assert srv.launched == []


@pytest.mark.parametrize("code", [0, 130, 1, 143])
def test_only_sigkill_counts_never_a_quit_or_a_crash(wt, code):
    srv = _Srv({"a": _Inst(wt)})
    _kill(srv, "a", code=code)
    assert not revive.holds_offline("a")
    assert revive.auto_tick(srv, start=_sync) is None
    assert srv.launched == []


def test_no_exit_marker_counts_only_when_it_died_mid_turn(wt, monkeypatch):
    """No marker (a launch without the exit wrapper, e.g. the engine's first
    start): only the hooks' last word ``working`` says it was killed — a quit
    fires Stop/SessionEnd, a SIGKILL fires nothing."""
    srv = _Srv({"a": _Inst(wt), "b": _Inst(wt)})
    monkeypatch.setattr(revive, "died_mid_turn", lambda s, i, t: t == "a")
    _kill(srv, "a", code=None)
    _kill(srv, "b", code=None)
    assert revive.holds_offline("a")
    assert not revive.holds_offline("b")


def test_paused_or_setting_off_is_not_queued(wt):
    srv = _Srv({"p": _Inst(wt, status=Status.Paused), "a": _Inst(wt)})
    _kill(srv, "p")
    assert not revive.holds_offline("p")
    S.update_settings(general={"revive_on_start": False})
    _kill(srv, "a")
    assert not revive.holds_offline("a")


def test_off_under_pytest_by_default(wt, monkeypatch):
    monkeypatch.setattr(revive, "AUTO_ENABLED", None)
    srv = _Srv({"a": _Inst(wt)})
    _kill(srv, "a")
    assert not revive.holds_offline("a")


def test_env_kill_switch(wt, monkeypatch):
    from backend.web.core import restart

    monkeypatch.setattr(revive, "AUTO_ENABLED", None)

    monkeypatch.setattr(restart, "_under_pytest", lambda: False)
    monkeypatch.setenv("MINDFLOCK_REVIVE_ON_START", "0")
    assert revive._auto_on() is False
    monkeypatch.delenv("MINDFLOCK_REVIVE_ON_START")
    assert revive._auto_on() is True


# --------------------------------------------------------------------------- #
# memory, pacing, rate limit
# --------------------------------------------------------------------------- #
def test_held_while_memory_is_low_without_spending_its_budget(wt, monkeypatch):
    srv = _Srv({"a": _Inst(wt)})
    _kill(srv, "a")
    monkeypatch.setattr(revive.memory_pressure, "is_low", lambda **kw: True)
    for _ in range(5):
        assert revive.auto_tick(srv, start=_sync) is None
    assert srv.launched == []
    assert revive.holds_offline("a")  # still coming back: not announced
    assert revive._AUTO_HISTORY.get("a", []) == []
    monkeypatch.setattr(revive.memory_pressure, "is_low", lambda **kw: False)
    assert revive.auto_tick(srv, start=_sync) == "a"
    assert srv.launched == ["a"]


def test_one_at_a_time_and_a_gap_between_launches(wt):
    srv = _Srv({"a": _Inst(wt), "b": _Inst(wt)})
    _kill(srv, "a", now=100.0)
    _kill(srv, "b", now=200.0)
    started = []
    # The first launch is still in flight (start never ran it): nothing else.
    assert revive.auto_tick(srv, now=1000.0, start=lambda *a: started.append(a)) == "a"
    assert revive.auto_tick(srv, now=1010.0, start=_sync) is None
    # In flight done; then the gap.
    revive._auto_revive_one(*started[0][1:])
    assert (
        revive.auto_tick(srv, now=1000.0 + revive.GAP_SECONDS / 2, start=_sync) is None
    )
    assert revive.auto_tick(srv, now=1010.0, start=_sync) == "b"
    assert srv.launched == ["a", "b"]


def test_not_while_a_sweep_runs(wt, monkeypatch):
    srv = _Srv({"a": _Inst(wt)})
    _kill(srv, "a")
    monkeypatch.setattr(revive, "_sweep_running", lambda: True)
    assert revive.auto_tick(srv, start=_sync) is None
    assert srv.launched == []


def test_rate_limited_then_left_offline_with_the_reason(wt):
    srv = _Srv({"a": _Inst(wt)})
    t = 10_000.0
    for n in range(revive.AUTO_MAX_PER_WINDOW):
        _kill(srv, "a", now=t)
        assert revive.auto_tick(srv, now=t, start=_sync) == "a"
        t += 60.0
    _kill(srv, "a", now=t)
    assert revive.auto_tick(srv, now=t, start=_sync) is None
    assert srv.launched == ["a"] * revive.AUTO_MAX_PER_WINDOW
    st = revive.auto_state("a")
    assert st["state"] == revive.AUTO_GAVE_UP
    assert "Killed 4 times in the last hour" in st["reason"]
    # Given up: its Offline is announced like any other (needs a human).
    assert not revive.holds_offline("a", now=time.time() + 60)
    # A person's Restart brings it back: the reason clears with a live reading.
    revive.observe(srv, "a", srv.ENGINE.instances["a"], "idle")
    assert revive.auto_state("a") is None


def test_the_window_slides(wt):
    srv = _Srv({"a": _Inst(wt)})
    t = 10_000.0
    for _ in range(revive.AUTO_MAX_PER_WINDOW):
        _kill(srv, "a", now=t)
        revive.auto_tick(srv, now=t, start=_sync)
        t += 60.0
    t += revive.AUTO_WINDOW_SECONDS
    _kill(srv, "a", now=t)
    assert revive.auto_tick(srv, now=t, start=_sync) == "a"


# --------------------------------------------------------------------------- #
# the skip rules, re-checked at launch time
# --------------------------------------------------------------------------- #
def test_skip_rules_are_rechecked_at_launch(wt):
    inst = _Inst(wt)
    srv = _Srv({"a": inst})
    _kill(srv, "a")
    inst.Status = Status.Paused  # paused while it waited for memory
    revive.auto_tick(srv, start=_sync)
    assert srv.launched == []
    assert not revive.holds_offline("a", now=time.time() + 60)
    # Not launched: its budget is untouched.
    assert revive._AUTO_HISTORY.get("a", []) == []


def test_a_failed_relaunch_says_why(wt):
    srv = _Srv({"a": _Inst(wt)}, fail={"a": "workspace no longer exists"})
    _kill(srv, "a")
    revive.auto_tick(srv, start=_sync)
    st = revive.auto_state("a")
    assert st["state"] == revive.AUTO_GAVE_UP
    assert "workspace no longer exists" in st["reason"]


# --------------------------------------------------------------------------- #
# the emitter: no bell / toast / push for a trip through Offline
# --------------------------------------------------------------------------- #
def test_the_offline_transition_is_held_while_it_comes_back(wt, monkeypatch):
    from backend.web import server
    from backend.web.core import events as _events

    seen = []
    monkeypatch.setattr(
        _events.BUS,
        "emit",
        lambda event, **kw: seen.append((event, kw.get("old"), kw.get("new"))),
    )
    title = "auto-revive-emit-test"
    server._EVENT_SNAPSHOT.pop(title, None)
    srv = _Srv({title: _Inst(wt)})
    try:
        server._emit_state_changes(title, "running", "working", "agent")
        _kill(srv, title)
        server._emit_state_changes(title, "running", "offline", "agent")
        assert not [e for e in seen if e[0] == "session.activity_changed"]
        # Given up (here: the setting went off and the queue was dropped):
        # the next reading announces it.
        revive._auto_reset_for_tests()
        server._emit_state_changes(title, "running", "offline", "agent")
        assert ("session.activity_changed", "working", "offline") in seen
    finally:
        server._EVENT_SNAPSHOT.pop(title, None)
