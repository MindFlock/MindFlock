"""A failed tmux probe is not a dead agent (``agent_state._agent_activity``).

"offline" used to be the answer to ANY failed probe: a ``has-session`` that
timed out under load (``run_capped``'s exit 124), a tmux error, an exception
anywhere in the layers. The emitter announces a move to offline at once (a
default-on push) and ``revive.observe`` read the first one as a death. Now:

* an indeterminate probe keeps the last known reading (and says so);
* a definite miss reads "offline" only once a SECOND one, at least
  ``_OFFLINE_CONFIRM_S`` later, confirms it;
* a session never seen live reads "offline" at once (nothing to flicker from).

All through mocks: no tmux is run.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from backend.web.core import agent_state
from backend.web.core import agent_sessions
from backend.web.core import proc

TITLE = "debounce-t"


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def time(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    # _missing_reading reads time.time() off agent_state's own `time` import.
    monkeypatch.setattr(agent_state, "time", SimpleNamespace(time=c.time))
    return c


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for d in (
        agent_state._ACTIVITY_CACHE,
        agent_state._MISSES,
        agent_state._LAST_PROBE,
        agent_state._PROBE_LOGGED_AT,
    ):
        d.pop(TITLE, None)
    yield
    for d in (
        agent_state._ACTIVITY_CACHE,
        agent_state._MISSES,
        agent_state._LAST_PROBE,
        agent_state._PROBE_LOGGED_AT,
    ):
        d.pop(TITLE, None)


def _inst():
    return SimpleNamespace(Started=lambda: True, Status=None, Program="claude")


def _seen(reading="working"):
    """This process saw the session live, reading ``reading``."""
    agent_state._ACTIVITY_CACHE[TITLE] = {
        "created": 1.0,
        "reported": reading,
        "state_since": 0.0,
        "worked_at": None,
        "source": "marker",
    }


def _probe(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr(
        agent_state._agent_sessions, "session_probe", lambda name: next(it)
    )


def _activity():
    return agent_state._agent_activity(_inst(), TITLE)


# --------------------------------------------------------------------------- #
# indeterminate: keep the last reading
# --------------------------------------------------------------------------- #
def test_a_has_session_timeout_keeps_the_last_reading(monkeypatch, clock):
    """run_capped turns a hung tmux into exit 124: no answer, not a death."""
    _seen("working")
    calls = []

    def run(args, **kw):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 124, b"", b"timed out after 10s")

    monkeypatch.setattr(proc, "run_capped", run)
    for _ in range(5):
        clock.t += 4.0
        assert _activity() == "working"
    assert calls[0] == ["tmux", "has-session", "-t=" + "mindflock_" + TITLE]
    assert agent_state.last_probe(TITLE) == "unknown"
    assert not agent_state.offline_confirmed(TITLE)


def test_a_tmux_error_that_is_not_a_missing_session_keeps_the_last_reading(
    monkeypatch, clock
):
    _seen("idle")
    monkeypatch.setattr(
        proc,
        "run_capped",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 1, b"", b"server exited unexpectedly\n"
        ),
    )
    assert _activity() == "idle"


def test_an_exception_in_the_layers_is_not_offline(monkeypatch, clock):
    """The old ``except Exception: return "offline"`` — silently."""
    _seen("clarify")
    _probe(monkeypatch, "live")

    def boom():
        raise RuntimeError("pane read blew up")

    monkeypatch.setattr(agent_state, "_server", boom)
    assert _activity() == "clarify"
    assert agent_state.last_probe(TITLE) == "unknown"


def test_with_no_earlier_reading_an_indeterminate_probe_reads_offline(
    monkeypatch, clock
):
    """Nothing to carry over — as before — but marked "unknown", so
    revive.observe does not count it as a death."""
    _probe(monkeypatch, "unknown")
    assert _activity() == "offline"
    assert agent_state.last_probe(TITLE) == "unknown"
    assert not agent_state.offline_confirmed(TITLE)


def test_an_indeterminate_probe_is_logged_once_a_minute(monkeypatch, clock):
    _seen("working")
    lines = []
    from backend import log

    monkeypatch.setattr(
        log, "InfoLog", SimpleNamespace(Printf=lambda f, *a: lines.append(f % a))
    )
    _probe(monkeypatch, *["unknown"] * 4)
    for _ in range(3):
        clock.t += 10.0
        _activity()
    assert len(lines) == 1 and "inconclusive" in lines[0]
    clock.t += agent_state._PROBE_LOG_EVERY_S
    _activity()
    assert len(lines) == 2


# --------------------------------------------------------------------------- #
# definite misses: debounced
# --------------------------------------------------------------------------- #
def test_one_definite_miss_keeps_the_reading_the_second_confirms(monkeypatch, clock):
    _seen("working")
    _probe(monkeypatch, "gone", "gone", "gone")
    assert _activity() == "working"  # one miss: not yet
    assert not agent_state.offline_confirmed(TITLE)
    clock.t += agent_state._OFFLINE_CONFIRM_S
    assert _activity() == "offline"  # a second, independent one: gone
    assert agent_state.offline_confirmed(TITLE)
    clock.t += 4.0
    assert _activity() == "offline"


def test_two_misses_in_the_same_instant_do_not_confirm(monkeypatch, clock):
    """Two callers in one tick are not two independent observations."""
    _seen("idle")
    _probe(monkeypatch, "gone", "gone", "gone")
    assert _activity() == "idle"
    clock.t += 0.1
    assert _activity() == "idle"
    clock.t += agent_state._OFFLINE_CONFIRM_S
    assert _activity() == "offline"


def test_a_live_probe_between_misses_starts_over(monkeypatch, clock):
    _seen("working")
    _probe(monkeypatch, "gone", "live", "gone")
    # the live poll's layers are not under test here: stop right after the
    # probe with an exception, which keeps the reading (see above).
    monkeypatch.setattr(
        agent_state, "_server", lambda: (_ for _ in ()).throw(RuntimeError("x"))
    )
    assert _activity() == "working"
    clock.t += 4.0
    assert _activity() == "working"  # live → the miss count is dropped
    assert agent_state._MISSES.get(TITLE) is None
    clock.t += 4.0
    assert _activity() == "working"  # a fresh first miss
    assert not agent_state.offline_confirmed(TITLE)


def test_a_timeout_between_misses_neither_confirms_nor_resets(monkeypatch, clock):
    _seen("working")
    _probe(monkeypatch, "gone", "unknown", "gone")
    assert _activity() == "working"
    clock.t += 4.0
    assert _activity() == "working"
    clock.t += 4.0
    assert _activity() == "offline"


def test_never_seen_live_reads_offline_at_once(monkeypatch, clock):
    _probe(monkeypatch, "gone")
    assert _activity() == "offline"
    assert agent_state.offline_confirmed(TITLE)


def test_a_confirmed_gone_session_stays_offline_through_a_timeout(monkeypatch, clock):
    _seen("working")
    _probe(monkeypatch, "gone", "gone", "unknown")
    _activity()
    clock.t += 4.0
    assert _activity() == "offline"
    clock.t += 4.0
    assert _activity() == "offline"  # not back to "working" on no answer


def test_the_real_probe_reads_cant_find_session_as_a_definite_miss(monkeypatch, clock):
    _seen("working")
    monkeypatch.setattr(
        proc,
        "run_capped",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 1, b"", b"can't find session: mindflock_debounce-t\n"
        ),
    )
    assert _activity() == "working"
    clock.t += 4.0
    assert _activity() == "offline"
    assert agent_state.last_probe(TITLE) == agent_sessions.PROBE_GONE


def test_paused_is_still_offline_at_once(monkeypatch, clock):
    """Only the tmux probe is debounced: a session paused on purpose is not
    a flicker."""
    from backend import session

    _seen("working")
    inst = SimpleNamespace(Started=lambda: True, Status=session.Paused)
    assert agent_state._agent_activity(inst, TITLE) == "offline"
