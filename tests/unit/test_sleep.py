"""Sleeping agents (``backend.web.core.sleep``): an idle, unwatched agent's
CLI and MCP servers are stopped to give their memory back, and woken —
resuming the conversation — by anything that needs it.

Pinned here:

* the eligibility rules, one veto at a time (and what an explicit sleep skips);
* the threshold: ``general.sleep_idle_minutes`` (default 45, 0 = off), 5 min
  while memory is low, and the settings allow-list;
* sleeping: the record is written BEFORE the kill, the exit code is cleared,
  a terminal that attached meanwhile stops it; the sweep picks the longest
  idle, caps a pass, names memory pressure, and heals a stale record;
* the record vs revive: the startup sweep skips sleepers (``sleeping``), a
  sleeper is never "died mid-turn", and ``_ensure_agent_session`` RESUMES it
  whatever exit code the hangup left, ending the sleep;
* the emitter swallows the sleep and the wake back to idle (no "offline"
  bell/toast/push), and announces anything else out of it;
* wake routing: ``/send``, Send now, the queue drain, the mailbox lane,
  ``delivery: "now"`` and MindFlock's composed messages all wake a sleeper
  and leave the text for the queue / lane, never typed into a booting CLI;
  ``/wake`` and ``/sleep`` routes.

Nothing here touches tmux, /proc or a real agent: the server slice is faked,
or the real module's launch/kill seams are patched.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.providers import activity_markers
from backend.session.storage import Status
from backend.web.core import events as events_mod
from backend.web.core import mailbox
from backend.web.core import ownership
from backend.web.core import prompt_queue
from backend.web.core import revive
from backend.web.core import sleep
from backend.web.core import team_runs

NOW = 1_800_000_000.0
#: The real probe, before the autouse fixture swaps it for "nobody attached".
_REAL_OUTSIDE_CLIENTS = sleep._outside_clients
IDLE_S = 45 * 60.0


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _Inst:
    def __init__(
        self,
        *,
        status=Status.Ready,
        started=True,
        peer="",
        in_place=False,
        program="claude",
        wt="/wt",
    ):
        self.Status = status
        self._started = started
        self.PeerShare = peer
        self.InPlace = in_place
        self.Program = program
        self._wt = wt

    def Started(self):
        return self._started

    def GetWorktreePath(self):
        return self._wt


class _Srv(SimpleNamespace):
    """The slice of ``backend.web.server`` the sleep module reads."""

    def __init__(self, instances=None, live=()):
        super().__init__()
        self.ENGINE = SimpleNamespace(
            instances=dict(instances or {}), lock=threading.RLock()
        )
        self._HUMAN_INPUT_AT = {}
        self._QUEUE_STATE = {}
        self._MAIL_STATE = {}
        self.live = set(live)
        self.activity = {}
        self.background = {}
        self.no_agent = set()
        self.dialogs = set()
        self.autopilot = set()
        self.tmux_typed = set()
        self.killed = []
        self.cleared = []
        self.republished = []
        self.launched = []
        self.sleeping_at_kill = []

    def _live_session_name(self, name):
        return name if name in {"mindflock_" + t for t in self.live} else None

    def _tmux_client_input_recent(self, title, within):
        return title in self.tmux_typed

    def _agent_activity(self, inst, title):
        return self.activity.get(title, "idle")

    def _background_jobs(self, inst, title):
        return self.background.get(title)

    def _pane_holds_agent(self, name, program=None):
        return name not in {"mindflock_" + t for t in self.no_agent}

    def _dialog_on_screen(self, inst, name):
        return name in {"mindflock_" + t for t in self.dialogs}

    def _autopilot_running(self, title):
        return title in self.autopilot

    def _kill_agent_session(self, title):
        self.sleeping_at_kill.append(sleep.is_sleeping(title))
        self.killed.append(title)
        self.live.discard(title)

    def _clear_exit_marker(self, name):
        self.cleared.append(name)

    def _republish_session(self, title):
        self.republished.append(title)

    def _ensure_agent_session(self, inst, title):
        self.launched.append(title)
        self.live.add(title)
        sleep.woke(title)  # what the real one does for a sleeper
        return "mindflock_" + title, None


def _row(title="a", **kw):
    r = {
        "title": title,
        "activity": "idle",
        "activity_since": NOW - 2 * IDLE_S,
        "background": None,
        "setup": None,
        "workspace_missing": False,
        "parent": "",
    }
    r.update(kw)
    return r


@pytest.fixture(autouse=True)
def _quiet_probes(monkeypatch):
    """No tmux, no /proc, no hook markers, nobody attached, no team runs."""
    monkeypatch.setattr(sleep, "_outside_clients", lambda name: 0)
    monkeypatch.setattr(sleep, "_pane_tree", lambda name: [])
    monkeypatch.setattr(activity_markers, "read_activity_marker", lambda name: "idle")
    monkeypatch.setattr(team_runs, "owner_of_title", lambda title, exclude_run="": None)
    sleep._WOKE_AT.clear()
    sleep._WAKE_WHY.clear()
    yield
    sleep._WOKE_AT.clear()
    sleep._WAKE_WHY.clear()


def _veto(srv, inst, title="a", row=None, *, rows=(), manual=False, now=NOW):
    return sleep.veto(
        srv, inst, title, row or _row(title), now, IDLE_S, rows=rows, manual=manual
    )


# --------------------------------------------------------------------------- #
# eligibility
# --------------------------------------------------------------------------- #
def test_an_idle_unwatched_agent_with_nothing_waiting_may_sleep():
    srv = _Srv(live={"a"})
    assert _veto(srv, _Inst()) is None


@pytest.mark.parametrize(
    "inst, want",
    [
        (_Inst(status=Status.Paused), sleep.V_STATUS),
        (_Inst(status=Status.Loading), sleep.V_STATUS),
        (_Inst(started=False), sleep.V_STATUS),
        (_Inst(peer="share-1"), sleep.V_PEER),
        (_Inst(in_place=True), sleep.V_IN_PLACE),
    ],
)
def test_the_session_itself_vetoes(inst, want):
    assert _veto(_Srv(live={"a"}), inst) == want


def test_a_cli_that_cannot_resume_never_sleeps(monkeypatch):
    """Sleeping is only safe when the wake brings the conversation back."""
    fake = SimpleNamespace(launcher_spec=lambda: SimpleNamespace(resume_flag=""))
    monkeypatch.setattr(sleep.providers, "resolve", lambda program: fake)
    assert _veto(_Srv(live={"a"}), _Inst(program="cline")) == sleep.V_NO_RESUME


def test_already_asleep():
    sleep.mark("a", sleep.REASON_IDLE)
    assert _veto(_Srv(live={"a"}), _Inst()) == sleep.V_ASLEEP


@pytest.mark.parametrize(
    "row, want",
    [
        (_row(activity="working"), sleep.V_NOT_IDLE),
        (_row(activity="clarify"), sleep.V_NOT_IDLE),
        (_row(activity="limit"), sleep.V_NOT_IDLE),
        (_row(activity="offline"), sleep.V_NOT_IDLE),
        (_row(workspace_missing=True), sleep.V_WORKSPACE),
        (_row(background=[{"pid": 1, "state": "running"}]), sleep.V_BACKGROUND),
        (_row(setup={"state": "running"}), sleep.V_SETUP),
        (_row(activity_since=NOW - 60), sleep.V_RECENT),
        (_row(activity_since=0), sleep.V_RECENT),
    ],
)
def test_the_row_vetoes(row, want):
    assert _veto(_Srv(live={"a"}), _Inst(), row=row) == want


def test_a_queued_prompt_vetoes():
    prompt_queue.enqueue("a", "next task")
    assert _veto(_Srv(live={"a"}), _Inst()) == sleep.V_QUEUE


def test_a_message_waiting_for_it_vetoes():
    mailbox.post("a", "your review is ready", sender="b")
    assert _veto(_Srv(live={"a"}), _Inst()) == sleep.V_MAIL


def test_a_team_run_member_or_lead_vetoes(monkeypatch):
    monkeypatch.setattr(
        team_runs, "owner_of_title", lambda title, exclude_run="": ({"id": "r"}, {})
    )
    assert _veto(_Srv(live={"a"}), _Inst()) == sleep.V_TEAM


def test_an_orchestrator_whose_workers_are_busy_vetoes():
    rows = [_row("a"), _row("w1", parent="a", activity="working")]
    assert _veto(_Srv(live={"a"}), _Inst(), rows=rows) == sleep.V_WORKERS
    # Workers that are idle (or asleep) leave it free to sleep.
    rows = [
        _row("a"),
        _row("w1", parent="a"),
        _row("w2", parent="a", activity="offline"),
    ]
    assert _veto(_Srv(live={"a"}), _Inst(), rows=rows) is None


def test_a_window_sharing_its_folder_sleeps_only_if_its_conversation_is_known(
    monkeypatch,
):
    """foo and foo-copy share one folder: a wake that cannot name foo's own
    conversation would resume the folder's newest — maybe foo-copy's."""
    from backend.providers import thread_markers

    srv = _Srv({"a": _Inst(), "a-copy": _Inst(in_place=True)}, live={"a"})
    monkeypatch.setattr(thread_markers, "read", lambda name: "")
    assert _veto(srv, _Inst()) == sleep.V_THREAD
    monkeypatch.setattr(
        thread_markers, "read", lambda name: "conv-a" if name == "mindflock_a" else ""
    )
    assert _veto(srv, _Inst()) is None
    # Alone in its folder, the folder's newest conversation IS its own.
    monkeypatch.setattr(thread_markers, "read", lambda name: "")
    assert _veto(_Srv({"a": _Inst()}, live={"a"}), _Inst()) is None


def test_fast_track_mid_chain_vetoes():
    srv = _Srv(live={"a"})
    srv.autopilot.add("a")
    assert _veto(srv, _Inst()) == sleep.V_AUTOPILOT


def test_recent_human_input_queue_send_mail_and_wake_veto():
    srv = _Srv(live={"a"})
    srv._HUMAN_INPUT_AT["a"] = NOW - 60
    assert _veto(srv, _Inst()) == sleep.V_TYPED
    srv = _Srv(live={"a"})
    srv._QUEUE_STATE["a"] = {"sent_at": NOW - 60}
    assert _veto(srv, _Inst()) == sleep.V_RECENT
    srv = _Srv(live={"a"})
    srv._MAIL_STATE["a"] = {"sent_at": NOW - 60}
    assert _veto(srv, _Inst()) == sleep.V_RECENT
    srv = _Srv(live={"a"})
    sleep._WOKE_AT["a"] = NOW - 60
    assert _veto(srv, _Inst()) == sleep.V_WOKE


def test_an_open_terminal_vetoes(monkeypatch):
    cid = ownership.attach("a", "", "")
    try:
        assert _veto(_Srv(live={"a"}), _Inst()) == sleep.V_VIEWED
    finally:
        ownership.detach(cid)
    # A raw `tmux attach` / `mindflock attach` the web layer cannot see.
    monkeypatch.setattr(sleep, "_outside_clients", lambda name: 1)
    assert _veto(_Srv(live={"a"}), _Inst()) == sleep.V_VIEWED


def test_only_clients_from_outside_this_server_count_as_watching(monkeypatch):
    """The engine keeps a PTY attached to every session it started, and each
    web terminal is a tmux client too — both children of this server. Only
    the rest (``mindflock attach``, SSH, a raw ``tmux attach``) are people the
    web layer cannot see."""
    import os

    from backend.web.core import proc

    monkeypatch.setattr(
        proc,
        "run_capped",
        lambda args, **kw: SimpleNamespace(returncode=0, stdout=b"101\n202\n"),
    )
    parents = {101: os.getpid(), 202: 1}
    monkeypatch.setattr(sleep, "_ppid", lambda pid: parents[pid])
    assert _REAL_OUTSIDE_CLIENTS("mindflock_a") == 1
    parents[202] = os.getpid()
    assert _REAL_OUTSIDE_CLIENTS("mindflock_a") == 0
    monkeypatch.setattr(
        proc, "run_capped", lambda args, **kw: SimpleNamespace(returncode=1, stdout=b"")
    )
    assert _REAL_OUTSIDE_CLIENTS("mindflock_a") == 1  # unknown: someone is


def test_typing_through_a_raw_tmux_client_vetoes():
    srv = _Srv(live={"a"})
    srv.tmux_typed.add("a")
    assert _veto(srv, _Inst()) == sleep.V_TYPED


def test_no_live_agent_or_a_bare_shell_vetoes():
    assert _veto(_Srv(live=()), _Inst()) == sleep.V_NO_AGENT
    srv = _Srv(live={"a"})
    srv.no_agent.add("a")  # the user quit it: a shell holds the pane
    assert _veto(srv, _Inst()) == sleep.V_NO_AGENT


@pytest.mark.parametrize("marker", ["working", "clarify"])
def test_a_turn_the_hooks_say_has_not_ended_vetoes(monkeypatch, marker):
    monkeypatch.setattr(activity_markers, "read_activity_marker", lambda name: marker)
    assert _veto(_Srv(live={"a"}), _Inst()) == sleep.V_TURN


def test_the_live_rereads_veto_what_the_row_went_stale_on():
    srv = _Srv(live={"a"})
    srv.activity["a"] = "working"  # a turn started since the row was built
    assert _veto(srv, _Inst()) == sleep.V_NOT_IDLE
    srv = _Srv(live={"a"})
    srv.background["a"] = [{"pid": 9, "state": "waiting"}]
    assert _veto(srv, _Inst()) == sleep.V_BACKGROUND
    srv = _Srv(live={"a"})
    srv.dialogs.add("a")
    assert _veto(srv, _Inst()) == sleep.V_DIALOG


def test_an_explicit_sleep_skips_only_the_timing_vetoes():
    srv = _Srv(live={"a"})
    srv._HUMAN_INPUT_AT["a"] = NOW - 5
    srv.tmux_typed.add("a")
    row = _row(activity_since=NOW - 5)
    assert _veto(srv, _Inst(), row=row, manual=True) is None
    # ...but never the safety ones.
    assert (
        _veto(srv, _Inst(), row=_row(activity="working"), manual=True)
        == sleep.V_NOT_IDLE
    )
    prompt_queue.enqueue("a", "next")
    assert _veto(srv, _Inst(), row=row, manual=True) == sleep.V_QUEUE


# --------------------------------------------------------------------------- #
# the threshold and the setting
# --------------------------------------------------------------------------- #
def test_threshold_default_off_and_memory_pressure():
    assert S.load_settings().general.sleep_idle_minutes is None
    assert sleep.idle_minutes() == 45
    assert sleep.threshold_seconds("ok") == 45 * 60
    assert sleep.threshold_seconds("unknown") == 45 * 60
    assert sleep.threshold_seconds("low") == 5 * 60
    assert sleep.threshold_seconds("critical") == 5 * 60
    S.update_settings(general={"sleep_idle_minutes": 2})
    assert sleep.threshold_seconds("low") == 2 * 60  # never above the setting
    S.update_settings(general={"sleep_idle_minutes": 0})
    assert sleep.threshold_seconds("ok") is None
    assert sleep.threshold_seconds("critical") is None  # off means off
    S.update_settings(general={"sleep_idle_minutes": ""})
    assert sleep.idle_minutes() == 45


def test_the_settings_route_saves_it_and_the_sweep_reads_it():
    from backend.web import server

    r = TestClient(server.app).post(
        "/api/settings", json={"general": {"sleep_idle_minutes": "30"}}
    )
    assert r.status_code == 200, r.text
    assert S.load_settings().general.sleep_idle_minutes == 30
    assert sleep.threshold_seconds("ok") == 30 * 60


def test_it_is_a_local_setting_never_synced():
    from backend.web.core import settings_sync

    assert "sleep_idle_minutes" in settings_sync.LOCAL["general"]
    assert "sleep_idle_minutes" not in settings_sync.SYNCED.get("general", ())


# --------------------------------------------------------------------------- #
# putting to sleep
# --------------------------------------------------------------------------- #
def test_put_to_sleep_records_first_then_stops_the_agent():
    srv = _Srv(live={"a"})
    assert sleep.put_to_sleep(srv, _Inst(), "a", sleep.REASON_IDLE, 3000) is None
    assert srv.killed == ["a"]
    assert srv.sleeping_at_kill == [True]  # no reader ever sees it dead unrecorded
    assert srv.cleared == ["mindflock_a"]  # the hangup's exit code says nothing
    assert srv.republished == ["a"]
    info = sleep.info("a")
    assert info["reason"] == "idle" and info["since"] > 0


def test_a_terminal_that_attached_meanwhile_stops_the_sleep(monkeypatch):
    monkeypatch.setattr(sleep, "_outside_clients", lambda name: 1)
    srv = _Srv(live={"a"})
    assert sleep.put_to_sleep(srv, _Inst(), "a", sleep.REASON_IDLE) == sleep.V_VIEWED
    assert srv.killed == [] and not sleep.is_sleeping("a")


def test_a_kill_that_did_not_take_leaves_no_record():
    srv = _Srv(live={"a"})
    srv._kill_agent_session = lambda title: None  # tmux refused
    assert sleep.put_to_sleep(srv, _Inst(), "a", sleep.REASON_IDLE)
    assert not sleep.is_sleeping("a")


def test_the_record_survives_a_reload_and_is_dropped_with_the_session():
    sleep.mark("a", sleep.REASON_MEMORY, 780)
    sleep._CACHE = None  # a fresh process
    assert sleep.info("a")["freed_mb"] == 780
    sleep.drop("a")
    assert sleep.info("a") is None
    sleep.mark("b", sleep.REASON_IDLE)
    sleep.prune(["c"])
    assert not sleep.is_sleeping("b")


def _tick_world(monkeypatch, rows, level="ok"):
    monkeypatch.setattr(
        events_mod, "sessions_snapshot", lambda: [dict(r) for r in rows]
    )
    monkeypatch.setattr(sleep.memory_pressure, "level", lambda **kw: level)
    titles = [r["title"] for r in rows]
    return _Srv({t: _Inst(wt="/wt/" + t) for t in titles}, live=set(titles))


def test_the_sweep_sleeps_the_longest_idle_first_and_caps_a_pass(monkeypatch):
    rows = [
        _row("t%d" % i, activity_since=NOW - IDLE_S - 60 * (i + 1)) for i in range(5)
    ]
    srv = _tick_world(monkeypatch, rows)
    slept = sleep.tick(srv, NOW)
    assert slept == ["t4", "t3", "t2"]  # MAX_PER_PASS, longest idle first
    assert all(sleep.info(t)["reason"] == "idle" for t in slept)
    assert not sleep.is_sleeping("t0")


def test_the_sweep_sleeps_sooner_under_memory_pressure_and_says_so(monkeypatch):
    rows = [_row("a", activity_since=NOW - 6 * 60)]
    srv = _tick_world(monkeypatch, rows, level="ok")
    assert sleep.tick(srv, NOW) == []  # 6 min < 45
    srv = _tick_world(monkeypatch, rows, level="low")
    assert sleep.tick(srv, NOW) == ["a"]
    assert sleep.info("a")["reason"] == "memory"


def test_the_sweep_is_off_at_zero(monkeypatch):
    S.update_settings(general={"sleep_idle_minutes": 0})
    srv = _tick_world(
        monkeypatch, [_row("a", activity_since=NOW - 10 * IDLE_S)], "critical"
    )
    assert sleep.tick(srv, NOW) == [] and srv.killed == []


def test_the_sweep_never_touches_remote_rows(monkeypatch):
    rows = [_row("rig::a", device="rig")]
    srv = _tick_world(monkeypatch, rows)
    srv.ENGINE.instances = {}  # remote rows are not in this ENGINE
    assert sleep.tick(srv, NOW) == []


def test_the_sweep_heals_a_record_whose_agent_runs_again(monkeypatch):
    sleep.mark("a", sleep.REASON_IDLE)
    srv = _tick_world(monkeypatch, [_row("a", activity="working")])
    sleep.tick(srv, time.time() + sleep.HEAL_AFTER_S + 1)
    assert not sleep.is_sleeping("a")


# --------------------------------------------------------------------------- #
# the record vs revive and the launch rule
# --------------------------------------------------------------------------- #
class _ReviveSrv(SimpleNamespace):
    def __init__(self, marker=137):
        super().__init__()
        self.marker = marker

    def _read_exit_marker(self, name):
        return self.marker

    def _is_natural_exit(self, code):
        return code in (0, 130)

    def _session_limited_until(self, title):
        return 0.0

    def _live_limit_reset(self, provider, now, inst=None):
        return None

    def _live_session_name(self, name):
        return None


def test_the_startup_sweep_skips_sleepers(tmp_path):
    inst = _Inst(wt=str(tmp_path))
    srv = _ReviveSrv()
    assert revive.skip_reason(srv, inst, "a", NOW) is None
    sleep.mark("a", sleep.REASON_IDLE)
    assert revive.skip_reason(srv, inst, "a", NOW) == revive.SKIP_SLEEPING
    # "Restart all" never lists a sleeper, and would not wake one either.
    assert (
        revive.skip_reason(srv, inst, "a", NOW, include_quit=True)
        == revive.SKIP_SLEEPING
    )


def test_a_sleeper_never_died_mid_turn(monkeypatch):
    monkeypatch.setattr(
        activity_markers, "read_activity_marker", lambda name: "working"
    )
    srv = _ReviveSrv(marker=129)  # the hangup's code: unnatural
    assert revive.died_mid_turn(srv, _Inst(), "a") is True  # control
    sleep.mark("a", sleep.REASON_IDLE)
    assert revive.died_mid_turn(srv, _Inst(), "a") is False


class _FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stderr = b""


def test_ensure_agent_session_resumes_a_sleeper_whatever_the_exit_code(monkeypatch):
    """The hangup can leave a "clean" exit code behind; a sleeper must still
    come back in its own conversation, and its launch ends the sleep."""
    from backend.providers import thread_markers
    from backend.web.core import agent_sessions, proc

    calls = []

    def run(args, **kw):
        calls.append(list(args))
        return _FakeProc(1 if args[1] == "has-session" else 0)

    class _Prov:
        def is_natural_exit(self, code):
            return True  # the marker says "quit on purpose"

        def build_launch_command(self, ctx):
            self.ctx = ctx
            return "claude --resume x"

        def install_activity_hooks(self, wt, name):
            pass

    prov = _Prov()
    sleep.mark("a", sleep.REASON_IDLE)
    cleared = []
    monkeypatch.setattr(proc, "run_capped", run)
    monkeypatch.setattr(agent_sessions.providers, "resolve", lambda prog: prov)
    monkeypatch.setattr(agent_sessions, "_read_exit_marker", lambda name: 0)
    monkeypatch.setattr(agent_sessions, "_clear_exit_marker", lambda name: None)
    monkeypatch.setattr(agent_sessions, "_wrap_launch_cmd", lambda cmd, name: cmd)
    monkeypatch.setattr(agent_sessions, "apply_scroll_speed", lambda: None)
    monkeypatch.setattr(thread_markers, "clear", lambda name: cleared.append(name))
    inst = _Inst(wt=str(__import__("tempfile").gettempdir()))
    inst.LaunchArgs = ()
    name, err = agent_sessions._ensure_agent_session(inst, "a")
    assert err is None
    assert prov.ctx.resume is True
    assert cleared == []  # its conversation id is kept for the --resume
    assert not sleep.is_sleeping("a")
    assert any(c[1] == "new-session" for c in calls)


# --------------------------------------------------------------------------- #
# notifications: suppressed at the emitter
# --------------------------------------------------------------------------- #
@pytest.fixture
def bus(monkeypatch):
    from backend.web import server

    b = events_mod.EventBus()
    monkeypatch.setattr(events_mod, "BUS", b)
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 0.0)
    for t in ("zz", "dead"):
        server._EVENT_SNAPSHOT.pop(t, None)
    got = []
    b.subscribe(got.append)
    yield server, got
    for t in ("zz", "dead"):
        server._EVENT_SNAPSHOT.pop(t, None)


def _acts(got):
    return [
        (e["old"], e["new"]) for e in got if e["event"] == "session.activity_changed"
    ]


def test_a_sleep_and_the_wake_back_to_idle_announce_nothing(bus):
    server, got = bus
    server._emit_state_changes("zz", "ready", "idle", "agent")  # seed
    sleep.mark("zz", sleep.REASON_IDLE)
    server._emit_state_changes("zz", "ready", "offline", "agent")
    assert _acts(got) == []  # no "agent offline" bell / toast / push
    assert server._EVENT_SNAPSHOT["zz"]["activity"] == "sleeping"
    sleep.drop("zz")  # woken
    server._emit_state_changes("zz", "ready", "idle", "agent")
    server._emit_state_changes("zz", "ready", "idle", "agent")  # past the settle
    assert _acts(got) == []
    server._emit_state_changes("zz", "ready", "working", "agent")
    assert _acts(got) == [("idle", "working")]


def test_a_crash_still_announces_offline(bus):
    server, got = bus
    server._emit_state_changes("dead", "ready", "idle", "agent")
    server._emit_state_changes("dead", "ready", "offline", "agent")
    assert _acts(got) == [("idle", "offline")]


def test_a_woken_agent_that_meets_a_dialog_is_announced(bus):
    server, got = bus
    server._emit_state_changes("zz", "ready", "idle", "agent")
    sleep.mark("zz", sleep.REASON_IDLE)
    server._emit_state_changes("zz", "ready", "offline", "agent")
    sleep.drop("zz")
    server._emit_state_changes("zz", "ready", "clarify", "agent")
    server._emit_state_changes("zz", "ready", "clarify", "agent")
    assert _acts(got) == [("sleeping", "clarify")]


# --------------------------------------------------------------------------- #
# wake routing (the real server, launch seams patched)
# --------------------------------------------------------------------------- #
@pytest.fixture
def woken(monkeypatch):
    """A sleeping session ``zz`` in ENGINE; ``_ensure_agent_session`` records
    the launch instead of starting anything (and ends the sleep like the real
    one), and nothing types into tmux."""
    from backend.web import server

    inst = _Inst()
    monkeypatch.setitem(server.ENGINE.instances, "zz", inst)
    launched = []
    typed = []

    def _ensure(i, title):
        launched.append(title)
        sleep.woke(title)
        return "mindflock_" + title, None

    monkeypatch.setattr(server, "_ensure_agent_session", _ensure)
    monkeypatch.setattr(
        server, "_send_to_agent", lambda *a, **k: typed.append(a) or True
    )
    monkeypatch.setattr(server, "_republish_session", lambda title: None)
    sleep.mark("zz", sleep.REASON_IDLE)
    yield SimpleNamespace(server=server, inst=inst, launched=launched, typed=typed)
    for d in (server._QUEUE_STATE, server._MAIL_STATE):
        d.pop("zz", None)


def test_send_wakes_it_and_queues_the_prompt_first(woken):
    prompt_queue.enqueue("zz", "older item")
    r = TestClient(woken.server.app).post("/api/instances/zz/send", json={"text": "hi"})
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": False, "queued": True, "submitted": False, "woke": True}
    assert (
        woken.launched == ["zz"] and woken.typed == []
    )  # never typed into a booting CLI
    assert [i["text"] for i in prompt_queue.get_state("zz")["items"]] == [
        "hi",
        "older item",
    ]
    assert not sleep.is_sleeping("zz")
    # The wake opened the queue's boot grace: the drain waits for the prompt.
    assert woken.server._QUEUE_STATE["zz"]["rebooted_at"] > 0


def test_an_unsubmitted_paste_only_wakes_it(woken):
    r = TestClient(woken.server.app).post(
        "/api/instances/zz/send", json={"text": "draft", "submit": False}
    )
    assert r.status_code == 409 and r.json()["waking"] is True
    assert woken.launched == ["zz"] and prompt_queue.get_state("zz")["items"] == []


def test_a_mindflock_composed_message_wakes_it_too(woken):
    told, reason = woken.server._deliver_to_agent(woken.inst, "zz", "scope note")
    assert (told, reason) == ("queued", None)
    assert woken.launched == ["zz"]
    assert [i["text"] for i in prompt_queue.get_state("zz")["items"]] == ["scope note"]


def test_send_now_wakes_it_and_puts_the_item_first(woken):
    prompt_queue.enqueue("zz", "a")
    item = prompt_queue.enqueue("zz", "b")["items"][-1]
    r = TestClient(woken.server.app).post(
        "/api/instances/zz/queue/send_now", json={"id": item["id"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["woke"] is True
    assert [i["text"] for i in prompt_queue.get_state("zz")["items"]] == ["b", "a"]
    assert woken.typed == []


def test_the_queue_drain_wakes_a_sleeper(woken, monkeypatch):
    prompt_queue.enqueue("zz", "next")
    monkeypatch.setattr(woken.server, "_agent_activity", lambda inst, title: "offline")
    woken.server._drain_one_queue("zz")
    assert "zz" in woken.launched and not sleep.is_sleeping("zz")
    assert woken.typed == []  # the send comes on a later, idle pass


def test_the_mailbox_lane_wakes_a_sleeping_recipient(woken):
    """MCP send_message stores the message; the lane wakes the recipient and
    types it once the relaunched CLI is ready — never into the boot."""
    msg = mailbox.post("zz", "worker done", sender="w1")
    woken.server._drain_one_mailbox("zz")
    assert woken.launched == ["zz"] and woken.typed == []
    assert mailbox.get("zz", msg["id"])["state"] == "pending"
    assert not sleep.is_sleeping("zz")


def test_delivery_now_wakes_and_leaves_it_to_the_lane(woken):
    msg = mailbox.post("zz", "now please", sender="w1")
    result, detail = woken.server._mail_deliver_now(woken.inst, "zz", msg)
    assert result == "pending" and detail == woken.server._MSG_WAKING
    assert woken.launched == ["zz"] and woken.typed == []


def test_opening_the_terminal_wakes_it(woken, monkeypatch):
    """The terminal websocket's first step is the wake."""
    seen = []
    monkeypatch.setattr(
        sleep,
        "ensure_awake",
        lambda inst, title, why, srv=None: seen.append(why) or (True, "stop here"),
    )
    with TestClient(woken.server.app).websocket_connect(
        "/api/instances/zz/terminal"
    ) as ws:
        assert "stop here" in ws.receive_text()
    assert seen == ["terminal opened"]


def test_wake_route(woken):
    c = TestClient(woken.server.app)
    assert c.post("/api/instances/zz/wake").json() == {"title": "zz", "woke": True}
    assert c.post("/api/instances/zz/wake").json() == {"title": "zz", "woke": False}
    assert woken.launched == ["zz"]
    assert c.post("/api/instances/nope/wake").status_code == 404


def test_sleep_route_refuses_with_the_veto(woken, monkeypatch):
    sleep.drop("zz")
    monkeypatch.setattr(
        events_mod, "sessions_snapshot", lambda: [_row("zz", activity="working")]
    )
    r = TestClient(woken.server.app).post("/api/instances/zz/sleep")
    assert r.status_code == 409
    assert r.json()["veto"] == sleep.V_NOT_IDLE
    assert not sleep.is_sleeping("zz")


# --------------------------------------------------------------------------- #
# the MCP's wait reads a sleeper as finished
# --------------------------------------------------------------------------- #
def test_wait_for_session_reads_a_sleeper_as_idle_not_offline():
    from backend.mcp import tools

    obs = tools.Toolbox.__new__(tools.Toolbox)
    obs.dispatched = {}
    obs.wait_poll_s = 1.0
    obs._settle_for = lambda row, override: 10
    obs._has_pending_messages = lambda title: False
    st = {"busy": False, "idle_seen": None, "offline_seen": None}
    row = {
        "title": "zz",
        "status": "ready",
        "activity": "offline",
        "activity_since": time.time() - 3600,
        "sleeping": {"since": time.time() - 600, "reason": "idle", "freed_mb": 0},
    }
    assert obs._observe("zz", row, st, time.time(), None) == "idle"
    assert "sleeping" in tools._COMPACT_OPTIONAL
