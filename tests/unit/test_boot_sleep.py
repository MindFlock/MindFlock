"""Asleep after a shutdown (``revive.hold_on_boot`` + ``core.sleep``
``REASON_SHUTDOWN``): the laptop was turned off, so the sessions come back
Sleeping, not held Offline — while a real crash is still held.

Owner, 2026-11-23: every session read "Offline" the morning after the laptop
was switched off, though nothing was broken. Reviving them all is not the fix
(after a crash under memory pressure that refilled the memory that had just
run out); a sleep is: no agent starts until a person opens one.

Pinned here, against fakes (nothing touches tmux or launches an agent):

* the decision, branch by branch: calm memory history → asleep; no exit
  marker but history ending in distress (replayed critical, logged low, OOM
  kills, distress pending on the last line) → held; exit 137 / a crash exit →
  held whatever the history; NO history → asleep (the chosen fail
  direction); history that ends long before the last orderly stop says
  nothing; distress older than the window does not count; an existing hold
  stays; ``revive_on_start`` on → nothing slept;
* the history tail reader (whatever its age, across the rotated file);
* only a person wakes a boot-sleeper: automatic wakes (auto, the queue drain,
  the mailbox lane, a flight step, MindFlock's composed messages) leave it
  asleep with their items parked; a person's wake works;
* the continue: a boot-sleeper cut off mid-turn gets it queued on wake, an
  idle one does not; "mid-turn" is read as of the stop, not of now;
* the terminal wake goes through the launch gate, and a wait closes the
  socket 4410 with ``waiting_for_memory``;
* ``announce_held`` counts only holds; flights don't read a mid-turn
  boot-sleeper as a finished turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.providers import activity_markers
from backend.session import tmux
from backend.web.core import flight_driver
from backend.web.core import held
from backend.web.core import launch_gate
from backend.web.core import mailbox
from backend.web.core import memory_watch
from backend.web.core import prompt_queue
from backend.web.core import revive
from backend.web.core import sleep
from tests.unit.test_held_offline import _Inst, _Srv

NOW = time.time()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    revive._auto_reset_for_tests()
    monkeypatch.setattr(revive.memory_pressure, "is_low", lambda **kw: False)
    monkeypatch.setattr(revive, "_fleet_oom_kills", lambda: None)
    monkeypatch.setattr(revive, "_machine_oom_kills", lambda: 0)
    yield
    revive._auto_reset_for_tests()


@pytest.fixture
def wt(tmp_path):
    d = tmp_path / "wt"
    d.mkdir()
    return str(d)


# --------------------------------------------------------------------------- #
# memory history helpers
# --------------------------------------------------------------------------- #
def _sys(**kw):
    """One calm ``system`` reading (a 24 GB box with half of it free)."""
    row = {
        "total_mb": 24000,
        "available_mb": 12000,
        "psi_some_avg10": 0.1,
        "psi_full_avg10": 0.0,
        "psi_some_avg60": 0.1,
        "psi_full_avg60": 0.0,
        "swap_in_mb_s": 0.0,
        "oom_kills": 0,
        "level": "ok",
        "pending": None,
        "why": [],
    }
    row.update(kw)
    return row


def _write_history(lines, path=None):
    """``lines`` = ``[(at, system dict)]`` written the way memory_watch does
    (``{"at":…`` first, compact)."""
    path = path or memory_watch.history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for at, system in lines:
            fh.write(
                json.dumps(
                    {"at": at, "system": system, "sessions": {}, "others": []},
                    separators=(",", ":"),
                )
                + "\n"
            )


def _calm_until(last, minutes=10):
    """A line every 30 s for ``minutes`` up to ``last``, all calm."""
    n = int(minutes * 2)
    return [(last - 30.0 * (n - i), _sys()) for i in range(n + 1)]


#: The previous run's last line: last night (a laptop off for ~12 h).
LAST_NIGHT = NOW - 12 * 3600


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #
def test_a_clean_stop_with_calm_memory_comes_back_asleep(wt):
    _write_history(_calm_until(LAST_NIGHT))
    srv = _Srv({"a": _Inst(wt), "b": _Inst(wt)}, markers={"b": 129})
    out = revive.hold_on_boot(srv, now=NOW)
    assert sorted(out["asleep"]) == ["a", "b"] and out["held"] == []
    assert out["total"] == 0
    assert out["evidence"]["crash"] is False
    assert out["evidence"]["stopped_at"] == LAST_NIGHT
    for t in ("a", "b"):
        assert sleep.info(t)["reason"] == sleep.REASON_SHUTDOWN
        assert not held.is_held(t)
    assert srv.launched == []


@pytest.mark.parametrize(
    "tail",
    [
        # PSI full stalls past the critical line, sustained: the replay says
        # critical though the logged level (an older rule) said ok.
        [_sys(psi_full_avg60=20.0, psi_full_avg10=20.0) for _ in range(4)],
        # The live level as logged.
        [_sys(level="low", why=["swap-in 2 MB/s"])],
        # An OOM kill on the last lines.
        [_sys(oom_kills=1)],
    ],
    ids=["replayed-critical", "logged-low", "oom-kill"],
)
def test_no_marker_but_history_ending_in_distress_is_held(wt, tail):
    lines = _calm_until(LAST_NIGHT - 30.0 * len(tail))
    lines += [(LAST_NIGHT - 30.0 * (len(tail) - 1 - i), s) for i, s in enumerate(tail)]
    _write_history(lines)
    srv = _Srv({"a": _Inst(wt), "h": _Inst(wt)}, markers={"h": 129})
    out = revive.hold_on_boot(srv, now=NOW)
    assert sorted(out["held"]) == ["a", "h"] and out["asleep"] == []
    assert out["evidence"]["crash"] is True
    for t in ("a", "h"):
        assert held.info(t)["reason"] == held.REASON_MEMORY_STOP
        assert not sleep.is_sleeping(t)


def test_distress_pending_on_the_very_last_line_counts(wt):
    """A death can cut the 30 s sustain short: the last line's ``pending``."""
    lines = _calm_until(LAST_NIGHT - 30.0) + [(LAST_NIGHT, _sys(pending="critical"))]
    _write_history(lines)
    srv = _Srv({"a": _Inst(wt)})
    out = revive.hold_on_boot(srv, now=NOW)
    assert out["held"] == ["a"]


def test_distress_before_the_window_does_not_count(wt):
    """Memory read critical 20 minutes before the stop, then recovered."""
    early = [(LAST_NIGHT - 1200.0 + 30.0 * i, _sys(level="critical")) for i in range(4)]
    _write_history(early + _calm_until(LAST_NIGHT, minutes=15))
    srv = _Srv({"a": _Inst(wt)})
    out = revive.hold_on_boot(srv, now=NOW)
    assert out["asleep"] == ["a"]


@pytest.mark.parametrize(
    "code,reason", [(137, held.REASON_KILLED), (1, held.REASON_CRASH)]
)
def test_a_crash_marker_is_held_whatever_the_history(wt, code, reason):
    _write_history(_calm_until(LAST_NIGHT))
    srv = _Srv({"a": _Inst(wt)}, markers={"a": code})
    out = revive.hold_on_boot(srv, now=NOW)
    assert out["held"] == ["a"] and held.info("a")["reason"] == reason
    assert not sleep.is_sleeping("a")
    # The crash marker decided it: memory history was never needed.
    assert out["evidence"] is None


def test_no_history_at_all_reads_as_an_ordinary_shutdown(wt):
    """The chosen fail direction: a fresh install / the sampler off."""
    assert not os.path.exists(memory_watch.history_path())
    srv = _Srv({"a": _Inst(wt)})
    out = revive.hold_on_boot(srv, now=NOW)
    assert out["asleep"] == ["a"] and out["held"] == []
    assert out["evidence"]["crash"] is False
    assert out["evidence"]["stopped_at"] is None


def test_history_that_ends_long_before_an_orderly_stop_says_nothing(wt):
    """The sampler stopped writing (in distress) while the server ran on for
    hours, then stopped in an orderly way: the history doesn't describe the
    stop."""
    _write_history([(LAST_NIGHT - 30.0 * i, _sys(level="critical")) for i in range(4)])
    revive.note_clean_stop(now=LAST_NIGHT + 3 * 3600)
    srv = _Srv({"a": _Inst(wt)})
    out = revive.hold_on_boot(srv, now=NOW)
    assert out["asleep"] == ["a"]
    assert out["evidence"]["stopped_at"] == pytest.approx(LAST_NIGHT + 3 * 3600)


def test_an_orderly_stop_never_outweighs_distress_at_the_end(wt):
    """systemd stopping the server over an OOM kill is an orderly stop too."""
    _write_history(_calm_until(LAST_NIGHT - 30.0) + [(LAST_NIGHT, _sys(oom_kills=2))])
    revive.note_clean_stop(now=LAST_NIGHT + 5.0)
    srv = _Srv({"a": _Inst(wt)})
    assert revive.hold_on_boot(srv, now=NOW)["held"] == ["a"]


def test_an_existing_hold_stays_held_and_is_never_converted(wt):
    held.hold("old", held.REASON_NOT_RUNNING)
    srv = _Srv({"old": _Inst(wt), "new": _Inst(wt)})
    out = revive.hold_on_boot(srv, now=NOW)
    assert out["asleep"] == ["new"]
    assert held.info("old")["reason"] == held.REASON_NOT_RUNNING
    assert not sleep.is_sleeping("old")
    assert out["total"] == 1  # what announce_held will count


def test_revive_on_start_on_sleeps_nothing(wt):
    S.update_settings(general={"revive_on_start": True})
    srv = _Srv({"a": _Inst(wt)})
    out = revive.hold_on_boot(srv, now=NOW)
    assert out["asleep"] == [] and out["held"] == []
    assert not sleep.is_sleeping("a") and not held.is_held("a")


def test_a_hold_another_server_made_meanwhile_wins(wt, monkeypatch):
    """Two servers on one config dir: the other one held it while this boot
    was marking it asleep — a hold outranks a sleep."""
    real_mark = sleep.mark

    def mark_then_other_server_holds(title, reason, freed_mb=0, **kw):
        real_mark(title, reason, freed_mb, **kw)
        with held._STORE.transaction() as recs:
            recs[title] = {"since": time.time(), "reason": held.REASON_KILLED}

    monkeypatch.setattr(sleep, "mark", mark_then_other_server_holds)
    srv = _Srv({"a": _Inst(wt)})
    out = revive.hold_on_boot(srv, now=NOW)
    assert out["asleep"] == [] and held.is_held("a") and not sleep.is_sleeping("a")


def test_announce_held_counts_only_holds(wt):
    srv = _Srv({"a": _Inst(wt), "k": _Inst(wt)}, markers={"k": 137})
    out = revive.hold_on_boot(srv, now=NOW)
    assert out["total"] == 1 and out["asleep"] == ["a"]
    env = asyncio.run(revive.announce_held(srv))
    assert env is not None and env["data"]["titles"] == ["k"]
    held.release("k")
    assert asyncio.run(revive.announce_held(srv)) is None  # sleepers ring nothing


# --------------------------------------------------------------------------- #
# the history tail
# --------------------------------------------------------------------------- #
def test_the_tail_reads_the_last_stretch_whatever_its_age_across_rotation():
    path = memory_watch.history_path()
    _write_history([(1000.0 + 30 * i, _sys()) for i in range(10)], path + ".1")
    _write_history([(1300.0 + 30 * i, _sys()) for i in range(3)], path)
    rows = memory_watch.tail(before=10_000_000.0, seconds=300.0)
    assert [r["at"] for r in rows] == [1060.0 + 30 * i for i in range(11)]
    # Lines at/after ``before`` (this boot's own) are not the previous run's.
    rows = memory_watch.tail(before=1330.0, seconds=60.0)
    assert [r["at"] for r in rows] == [1240.0, 1270.0, 1300.0]
    assert memory_watch.tail(before=500.0, seconds=60.0) == []


def test_the_tail_skips_a_line_cut_in_half_and_garbage():
    path = memory_watch.history_path()
    _write_history([(2000.0 + 30 * i, _sys()) for i in range(50)])
    with open(path, "a") as fh:
        fh.write('{"at":9999.0,"system":{"tru\n')
        fh.write("not json\n")
    rows = memory_watch.tail(before=1e9, seconds=60.0, max_bytes=2000)
    assert [r["at"] for r in rows] == [3410.0, 3440.0, 3470.0]


# --------------------------------------------------------------------------- #
# only a person wakes a boot-sleeper
# --------------------------------------------------------------------------- #
@pytest.fixture
def booted(monkeypatch):
    """A boot-sleeper ``bs`` in ENGINE; launches are recorded (ending the
    sleep like the real ``_ensure_agent_session``), nothing types."""
    from backend.web import server
    from tests.unit.test_sleep import _Inst as _SInst

    inst = _SInst()
    monkeypatch.setitem(server.ENGINE.instances, "bs", inst)
    launched = []

    def _ensure(i, title):
        launched.append(title)
        sleep.woke(title)
        return "mindflock_" + title, None

    monkeypatch.setattr(server, "_ensure_agent_session", _ensure)
    monkeypatch.setattr(server, "_send_to_agent", lambda *a, **k: True)
    monkeypatch.setattr(server, "_republish_session", lambda title: None)
    monkeypatch.setattr(server, "_emit_queue_changed", lambda title: None)
    sleep.mark("bs", sleep.REASON_SHUTDOWN)
    yield type("B", (), {"server": server, "inst": inst, "launched": launched})
    for d in (server._QUEUE_STATE, server._MAIL_STATE):
        d.pop("bs", None)


def test_automatic_wakes_leave_a_boot_sleeper_asleep(booted):
    for kw in ({"auto": True}, {"person": False}):
        assert sleep.ensure_awake(booted.inst, "bs", "x", booted.server, **kw) == (
            False,
            sleep.BOOT_WAITS,
        )
    assert booted.launched == [] and sleep.boot_asleep("bs")
    # Nothing took a launch-gate admission on the way.
    assert launch_gate._ADMITTED == {} and launch_gate.waiting("bs") is None


def test_a_person_wakes_a_boot_sleeper(booted):
    assert sleep.ensure_awake(booted.inst, "bs", "Wake", booted.server) == (True, None)
    assert booted.launched == ["bs"] and not sleep.is_sleeping("bs")


def test_the_queue_drain_keeps_its_prompts_parked(booted, monkeypatch):
    prompt_queue.enqueue("bs", "from before the shutdown")
    monkeypatch.setattr(booted.server, "_agent_activity", lambda inst, title: "offline")
    # Memory is short: a drain that asked the gate would queue the session
    # there and its row would read "Waiting for memory" — it must not ask.
    monkeypatch.setattr(
        launch_gate, "_admit_locked", lambda key: (False, "memory is low", "memory")
    )
    monkeypatch.setattr(launch_gate, "_save", lambda: None)
    monkeypatch.setattr(launch_gate, "_ensure_drainer", lambda: None)
    try:
        booted.server._drain_one_queue("bs")
        assert launch_gate.waiting("bs") is None
    finally:
        launch_gate.cancel("bs")
    assert booted.launched == [] and sleep.boot_asleep("bs")
    assert [i["text"] for i in prompt_queue.get_state("bs")["items"]] == [
        "from before the shutdown"
    ]
    # A relaunch saved in the launch gate before the shutdown is refused too.
    from backend.web.core import queue_driver

    rec = {"armed": True, "sent_at": 0.0, "rebooted_at": 0.0, "idle_since": None}
    assert queue_driver._queue_relaunch("bs", rec) == sleep.BOOT_WAITS
    assert booted.launched == []


def test_the_mailbox_lane_and_delivery_now_leave_it_asleep(booted):
    msg = mailbox.post("bs", "worker done", sender="w1")
    booted.server._drain_one_mailbox("bs")
    assert booted.launched == [] and mailbox.get("bs", msg["id"])["state"] == "pending"
    result, detail = booted.server._mail_deliver_now(booted.inst, "bs", msg)
    assert (result, detail) == ("pending", booted.server._MSG_BOOT_ASLEEP)
    assert booted.launched == []


def test_a_mindflock_composed_message_is_queued_not_woken(booted):
    told, reason = booted.server._deliver_to_agent(
        booted.inst, "bs", "verify notice", auto=True
    )
    assert (told, reason) == ("queued", None)
    assert booted.launched == [] and sleep.boot_asleep("bs")
    assert [i["text"] for i in prompt_queue.get_state("bs")["items"]] == [
        "verify notice"
    ]


def test_a_persons_send_wakes_it(booted):
    r = TestClient(booted.server.app).post(
        "/api/instances/bs/send", json={"text": "hi"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["woke"] is True and booted.launched == ["bs"]


def test_a_flight_step_queues_without_waking_it(booted):
    flight_driver._deliver("bs", "step two")
    assert booted.launched == [] and sleep.boot_asleep("bs")
    assert [i["text"] for i in prompt_queue.get_state("bs")["items"]] == ["step two"]


def test_a_wake_saved_by_another_run_never_wakes_a_boot_sleeper(booted):
    later = sleep._restore_wake(
        {"title": "bs", "args": {"why": "terminal opened", "person": True}}
    )
    assert later is not None and later() == sleep.BOOT_WAITS
    assert booted.launched == []


# --------------------------------------------------------------------------- #
# the terminal wake goes through the launch gate
# --------------------------------------------------------------------------- #
def test_a_terminal_wake_waits_in_the_gate_and_the_socket_says_so(booted, monkeypatch):
    monkeypatch.setattr(
        launch_gate, "_admit_locked", lambda key: (False, "memory is low", "memory")
    )
    monkeypatch.setattr(launch_gate, "_save", lambda: None)
    monkeypatch.setattr(launch_gate, "_ensure_drainer", lambda: None)
    try:
        with TestClient(booted.server.app).websocket_connect(
            "/api/instances/bs/terminal"
        ) as ws:
            frame = json.loads(ws.receive_text())
            assert frame["waiting_for_memory"] is True
            assert frame["message"] == launch_gate.WAITING_FOR_MEMORY
            with pytest.raises(Exception) as closed:
                ws.receive_text()
        assert getattr(closed.value, "code", None) == 4410
        assert booted.launched == [] and launch_gate.waiting("bs") is not None
    finally:
        launch_gate.cancel("bs")


def test_a_terminal_wake_with_room_wakes_it_at_once(booted):
    name, err = booted.server.ensure_terminal_session(booted.inst, "bs")
    assert booted.launched[:1] == ["bs"] and not sleep.is_sleeping("bs")


# --------------------------------------------------------------------------- #
# the continue
# --------------------------------------------------------------------------- #
def test_a_mid_turn_boot_sleeper_gets_the_continue_on_wake(booted):
    sleep.mark("bs", sleep.REASON_SHUTDOWN, mid_turn=True)
    assert sleep.info("bs")["mid_turn"] is True
    assert sleep.ensure_awake(booted.inst, "bs", "terminal opened", booted.server)[0]
    items = prompt_queue.get_state("bs")["items"]
    assert [i["text"] for i in items] == [revive.CONTINUE_PROMPT]


def test_an_idle_boot_sleeper_gets_no_continue(booted):
    assert "mid_turn" not in sleep.info("bs")
    assert sleep.ensure_awake(booted.inst, "bs", "terminal opened", booted.server)[0]
    assert prompt_queue.get_state("bs")["items"] == []


def test_mid_turn_is_read_as_of_the_stop_not_of_now(wt, monkeypatch):
    """The agent's hooks said ``working`` at 23:00; the laptop went off and
    the boot is 12 h later — past the marker's 6 h freshness as of now."""
    name = tmux.to_mindflock_tmux_name("m")
    path = activity_markers.marker_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": "working", "ts": LAST_NIGHT - 60}))
    _write_history(_calm_until(LAST_NIGHT))
    srv = _Srv({"m": _Inst(wt), "i": _Inst(wt)}, markers={"m": 129, "i": 129})
    assert activity_markers.read_activity_marker(name) is None  # stale as of now
    assert activity_markers.read_activity_marker(name, now=LAST_NIGHT) == "working"
    revive.hold_on_boot(srv, now=NOW)
    assert sleep.info("m")["mid_turn"] is True
    assert "mid_turn" not in sleep.info("i")


def test_flights_do_not_read_a_mid_turn_boot_sleeper_as_a_finished_turn():
    assert flight_driver._asleep_turn_over({"sleeping": {"reason": "idle"}}) is True
    assert (
        flight_driver._asleep_turn_over(
            {"sleeping": {"reason": "shutdown", "mid_turn": True}}
        )
        is False
    )
    assert flight_driver._asleep_turn_over({"sleeping": None}) is False


def test_the_clean_stop_marker_round_trips():
    assert revive.clean_stop_at() is None
    revive.note_clean_stop(now=1234.5)
    assert revive.clean_stop_at() == 1234.5
