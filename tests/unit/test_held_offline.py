"""Held offline (``backend.web.core.held``): after a crash, a session whose
agent died stays down until a person presses Start.

Owner, 2026-11-04: the machine restarted twice under memory pressure, and on
each boot the revive sweep, the auto-revive and the panes' terminal sockets
relaunched every agent — straight back into the memory that had just run out.

Pinned here, against fakes (nothing touches tmux or launches an agent):

* the store: hold/release/is_held/held, the first record kept, persisted;
* when a session becomes held: at boot (``revive.hold_on_boot``) and after an
  unclean death while the server runs (``revive.observe``) — never a clean
  quit, a pause, a sleep;
* every relaunch-on-touch path refuses it like Paused: the helper
  (``HELD_REFUSED``), the terminal socket (4424), ``/send`` (409, the prompt
  QUEUED), Send now, the queue drain + limit watch, a wake, the sweep;
* explicit Start releases it: ``/revive`` and ``/revive-all``; removing,
  pausing it drops it; a failed Start keeps it;
* the row's ``held_offline`` / ``held_since`` / ``held_reason``;
* ``general.revive_on_start``: default off (held), on = the old behaviour;
* the one boot notice (``system.sessions_held`` + the ``sessions_held`` rule).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import ptyprocess
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.config import settings as S
from backend.session.storage import Status
from backend.web import server
from backend.web.core import agent_sessions
from backend.web.core import events
from backend.web.core import held
from backend.web.core import prompt_queue
from backend.web.core import queue_driver
from backend.web.core import revive
from backend.web.core import sleep
from backend.web.core.agent_sessions import HELD_REFUSED
from tests.unit.test_mailbox_routes import _mk_inst


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _Inst:
    def __init__(self, wt, *, status=Status.Running, peer="", program="claude"):
        self.Status = status
        self.PeerShare = peer
        self.InPlace = False
        self.Program = program
        self._wt = wt
        self.CreatedAt = self.UpdatedAt = None

    def Started(self):
        return True

    def GetWorktreePath(self):
        return self._wt


class _Srv(SimpleNamespace):
    """The slice of ``backend.web.server`` the boot hold, observe and the
    sweep use."""

    def __init__(self, instances, *, markers=None, alive=(), limited=(), fail=None):
        super().__init__()
        self.ENGINE = SimpleNamespace(instances=instances, lock=threading.RLock())
        self.markers = dict(markers or {})
        self.alive = set(alive)
        self.limited = set(limited)
        self.fail = dict(fail or {})
        self.launched = []
        self._BOOT_QUIET_SECONDS = 0.0
        self._BOOT_MONO = time.monotonic()

    def _read_exit_marker(self, name):
        for title, code in self.markers.items():
            if name.endswith("_" + title):
                return code
        return None

    def _is_natural_exit(self, code):
        return code in (0, 130)

    def _live_session_name(self, name):
        return name if any(name.endswith("_" + t) for t in self.alive) else None

    def _session_limited_until(self, title):
        return time.time() + 3600 if title in self.limited else 0.0

    def _live_limit_reset(self, provider, now, inst=None):
        return None

    def _ensure_agent_session(self, inst, title):
        self.launched.append(title)
        err = self.fail.get(title)
        if err is None:
            self.alive.add(title)
        return "mindflock_" + title, err

    def _republish_session(self, title):
        pass

    def _emit_queue_changed(self, title):
        pass


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    revive._auto_reset_for_tests()
    monkeypatch.setattr(revive.memory_pressure, "is_low", lambda **kw: False)
    monkeypatch.setattr(revive, "died_mid_turn", lambda srv, inst, title: False)
    yield
    revive._auto_reset_for_tests()


@pytest.fixture
def wt(tmp_path):
    d = tmp_path / "wt"
    d.mkdir()
    return str(d)


def _run(coro):
    return asyncio.run(coro)


class _Launching(Exception):
    """Raised where the real relaunch would begin resolving the provider."""


def _no_provider(monkeypatch):
    def boom(program):
        raise _Launching(program)

    monkeypatch.setattr(agent_sessions.providers, "resolve", boom)


@pytest.fixture
def h(tmp_path, monkeypatch):
    """One RUNNING session ``h`` held offline on an empty engine: its
    worktree on disk, no agent tmux alive. Launches are recorded no-ops."""
    d = tmp_path / "h"
    d.mkdir()
    monkeypatch.setattr(server.ENGINE, "instances", {})
    inst = _mk_inst("h", str(d), status=Status.Running)
    server.ENGINE.instances["h"] = inst
    monkeypatch.setattr(server, "_live_session_name", lambda name: None)
    monkeypatch.setattr(server, "_budget_locked", lambda title: False)
    monkeypatch.setattr(server, "_emit_queue_changed", lambda title: None)
    launched = []

    def ensure(i, title):
        launched.append(title)
        return "mindflock_" + title, None

    inst.launched = launched
    inst.ensure = ensure
    held.hold("h", held.REASON_KILLED)
    return inst


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def test_the_store_round_trips_and_survives_a_reload(tmp_path, monkeypatch):
    path = tmp_path / "x" / "held.json"
    path.parent.mkdir()
    monkeypatch.setenv("MINDFLOCK_HELD_FILE", str(path))
    assert held.held() == {} and not held.is_held("a")
    assert held.hold("a", held.REASON_RESTART) is True
    first = held.info("a")
    # Already held: the first record (when, why it went down) stays.
    assert held.hold("a", held.REASON_CRASH) is False
    assert held.info("a") == first
    assert held.hold("b", "something else") is True
    assert held.info("b")["reason"] == held.REASON_CRASH  # unknown → crashed
    assert set(held.held()) == {"a", "b"}
    # Persisted: a fresh process reads it back.
    monkeypatch.setattr(held, "_CACHE", None)
    assert json.loads(path.read_text())["a"]["reason"] == held.REASON_RESTART
    assert held.is_held("a") and held.info("a")["since"] == first["since"]
    assert held.release("a") is True and held.release("a") is False
    held.prune(["zzz"])
    assert held.held() == {}


def test_row_fields_are_omitted_unless_held():
    assert held.row_fields("a") == {}
    held.hold("a", held.REASON_KILLED)
    f = held.row_fields("a")
    assert f["held_offline"] is True and f["held_reason"] == held.REASON_KILLED
    assert abs(f["held_since"] - time.time()) < 5


def test_the_instance_row_carries_the_hold(h):
    from backend.web.core import snapshot

    row = snapshot._instance_json(h, cheap=True)
    assert row["held_offline"] is True
    assert row["held_reason"] == held.REASON_KILLED
    assert isinstance(row["held_since"], float)
    held.release("h")
    row = snapshot._instance_json(h, cheap=True)
    assert "held_offline" not in row and "held_reason" not in row


# --------------------------------------------------------------------------- #
# when a session becomes held: at boot
# --------------------------------------------------------------------------- #
def test_the_boot_holds_every_session_whose_agent_died(wt, tmp_path):
    gone = str(tmp_path / "gone")
    srv = _Srv(
        {
            "restarted": _Inst(wt),
            "killed": _Inst(wt),
            "quit": _Inst(wt),
            "paused": _Inst(wt, status=Status.Paused),
            "loading": _Inst(wt, status=Status.Loading),
            "asleep": _Inst(wt),
            "alive": _Inst(wt),
            "peer": _Inst(wt, peer="share-1"),
            "noworkspace": _Inst(gone),
            "limited": _Inst(wt),
        },
        markers={"killed": 137, "quit": 0},
        alive={"alive"},
        limited={"limited"},
    )
    sleep.mark("asleep", sleep.REASON_IDLE)
    out = revive.hold_on_boot(srv)
    assert sorted(out["held"]) == ["killed", "limited", "restarted"]
    assert out["total"] == 3
    assert held.info("restarted")["reason"] == held.REASON_RESTART
    assert held.info("killed")["reason"] == held.REASON_KILLED
    # A usage-limited session is held like any other: the limit watch would
    # otherwise relaunch it the moment the window reopens.
    assert held.info("limited")["reason"] == held.REASON_RESTART
    assert srv.launched == []


def test_the_boot_keeps_old_holds_and_drops_gone_sessions(wt):
    held.hold("a", held.REASON_KILLED)
    held.hold("deleted-meanwhile", held.REASON_CRASH)
    srv = _Srv({"a": _Inst(wt)})
    out = revive.hold_on_boot(srv)
    assert out["held"] == [] and out["total"] == 1
    assert held.info("a")["reason"] == held.REASON_KILLED  # the first record
    assert not held.is_held("deleted-meanwhile")


def test_with_revive_on_start_on_nothing_is_held(wt):
    """The opt-in old behaviour: holds are dropped and the sweep revives."""
    S.update_settings(general={"revive_on_start": True})
    held.hold("old", held.REASON_KILLED)
    srv = _Srv({"a": _Inst(wt), "old": _Inst(wt)})
    out = revive.hold_on_boot(srv)
    assert out == {"held": [], "total": 0, "released": 1}
    assert held.held() == {}
    swept = _run(revive.run_on_start(srv))
    assert sorted(swept["revived"]) == ["a", "old"]


def test_the_setting_defaults_off_so_the_startup_sweep_relaunches_nothing(wt):
    assert S.load_settings().general.revive_on_start is None
    assert revive.enabled() is False
    srv = _Srv({"a": _Inst(wt)})
    revive.hold_on_boot(srv)
    assert _run(revive.run_on_start(srv)) is None
    assert srv.launched == [] and held.is_held("a")


def test_the_boot_hold_is_off_under_pytest():
    assert server._hold_on_start_enabled() is False


# --------------------------------------------------------------------------- #
# when a session becomes held: an unclean death while the server runs
# --------------------------------------------------------------------------- #
def _die(srv, title, code):
    inst = srv.ENGINE.instances[title]
    srv.alive.add(title)
    revive.observe(srv, title, inst, "working")
    srv.alive.discard(title)
    srv.markers[title] = code
    revive.observe(srv, title, inst, "offline")


@pytest.mark.parametrize(
    "code,reason",
    [(137, held.REASON_KILLED), (1, held.REASON_CRASH), (143, held.REASON_CRASH)],
)
def test_an_unclean_death_is_held(wt, code, reason):
    srv = _Srv({"a": _Inst(wt)})
    _die(srv, "a", code)
    assert held.info("a")["reason"] == reason
    # …and nothing brings it back by itself.
    assert revive.auto_state("a") is None and not revive.holds_offline("a")
    assert revive.auto_tick(srv, start=lambda fn, *a: fn(*a)) is None
    assert srv.launched == []


def test_a_sigkill_with_no_exit_marker_mid_turn_is_held(wt, monkeypatch):
    monkeypatch.setattr(revive, "died_mid_turn", lambda s, i, t: t == "a")
    srv = _Srv({"a": _Inst(wt), "b": _Inst(wt)})
    _die(srv, "a", None)
    _die(srv, "b", None)  # no marker, not mid-turn: undecidable → not held
    assert held.info("a")["reason"] == held.REASON_KILLED
    assert not held.is_held("b")


@pytest.mark.parametrize("code", [0, 130])
def test_a_clean_quit_is_not_held(wt, code):
    srv = _Srv({"a": _Inst(wt)})
    _die(srv, "a", code)
    assert not held.is_held("a")


def test_a_sleep_a_pause_or_a_session_never_seen_live_is_not_held(wt):
    srv = _Srv(
        {"s": _Inst(wt), "p": _Inst(wt, status=Status.Paused), "boot": _Inst(wt)},
        markers={"boot": 137},
    )
    sleep.mark("s", sleep.REASON_IDLE)
    _die(srv, "s", 129)
    _die(srv, "p", 137)
    revive.observe(srv, "boot", srv.ENGINE.instances["boot"], "offline")
    assert held.held() == {}


def test_a_live_reading_drops_a_stale_hold(wt):
    """Started outside MindFlock (a tmux attach, the CLI): no longer held."""
    held.hold("a", held.REASON_RESTART)
    srv = _Srv({"a": _Inst(wt)})
    revive.observe(srv, "a", srv.ENGINE.instances["a"], "offline")
    assert held.is_held("a")
    revive.observe(srv, "a", srv.ENGINE.instances["a"], "idle")
    assert not held.is_held("a")


def test_with_the_setting_on_a_killed_agent_is_auto_revived_not_held(wt, monkeypatch):
    S.update_settings(general={"revive_on_start": True})
    monkeypatch.setattr(revive, "AUTO_ENABLED", True)
    srv = _Srv({"a": _Inst(wt)})
    _die(srv, "a", 137)
    assert not held.is_held("a")
    assert revive.auto_state("a")["state"] == revive.AUTO_PENDING


# --------------------------------------------------------------------------- #
# every relaunch-on-touch path refuses a held session
# --------------------------------------------------------------------------- #
def test_the_relaunch_helper_refuses_a_held_session(h, monkeypatch):
    _no_provider(monkeypatch)
    assert agent_sessions._ensure_agent_session(h, "h") == ("mindflock_h", HELD_REFUSED)


def test_a_held_session_whose_agent_is_alive_is_attached_and_released(h, monkeypatch):
    _no_provider(monkeypatch)
    monkeypatch.setattr(server, "_live_session_name", lambda name: name)
    assert agent_sessions._ensure_agent_session(h, "h") == ("mindflock_h", None)
    assert not held.is_held("h")


def test_once_released_the_helper_launches_again(h, monkeypatch):
    _no_provider(monkeypatch)
    held.release("h")
    with pytest.raises(_Launching):
        agent_sessions._ensure_agent_session(h, "h")


def test_the_terminal_socket_closes_4424_and_launches_nothing(h, monkeypatch):
    _no_provider(monkeypatch)  # the REAL helper runs: it must not get this far
    spawned = []
    monkeypatch.setattr(
        ptyprocess.PtyProcess, "spawn", lambda *a, **k: spawned.append(a)
    )
    c = TestClient(server.app)
    with c.websocket_connect("/api/instances/h/terminal") as ws:
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "error" and frame["message"] == HELD_REFUSED
        assert frame["held"] is True and frame["held_reason"] == held.REASON_KILLED
        assert "paused" not in frame
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == 4424
    assert spawned == [] and held.is_held("h")


def test_send_queues_the_prompt_and_answers_409_held(h, monkeypatch):
    _no_provider(monkeypatch)
    sent = []
    monkeypatch.setattr(server, "_send_to_agent", lambda *a, **k: sent.append(a))
    c = TestClient(server.app)
    r = c.post("/api/instances/h/send", json={"text": "fix the test"})
    assert r.status_code == 409
    assert r.json() == {"error": HELD_REFUSED, "held": True, "queued": True}
    r = c.post("/api/instances/h/send", json={"text": "and then", "dialog_safe": True})
    assert r.status_code == 409 and r.json()["queued"] is True
    # Never lost: both wait on the queue for after Start.
    items = prompt_queue.get_state("h")["items"]
    assert [i["text"] for i in items] == ["fix the test", "and then"]
    assert sent == [] and held.is_held("h")


def test_an_unsubmitted_text_or_an_enter_is_refused_not_queued(h, monkeypatch):
    _no_provider(monkeypatch)
    assert server._deliver_to_agent(h, "h", "draft", boot=True, submit=False) == (
        False,
        HELD_REFUSED,
    )
    assert server._enter_on_agent(h, "h") == (False, HELD_REFUSED)
    assert prompt_queue.get_state("h")["items"] == []


def test_a_composed_message_waits_on_the_queue(h, monkeypatch):
    """``boot=False`` (MindFlock's own messages): queued, never relaunched."""
    _no_provider(monkeypatch)
    assert server._deliver_to_agent(h, "h", "your fence moved") == ("queued", None)
    assert [i["text"] for i in prompt_queue.get_state("h")["items"]] == [
        "your fence moved"
    ]


def test_send_now_is_409_held_and_the_item_stays_queued(h, monkeypatch):
    _no_provider(monkeypatch)
    item = prompt_queue.enqueue("h", "later")
    c = TestClient(server.app)
    r = c.post("/api/instances/h/queue/send_now", json={"id": item["items"][0]["id"]})
    assert r.status_code == 409
    assert r.json() == {"error": HELD_REFUSED, "held": True}
    assert [i["text"] for i in prompt_queue.get_state("h")["items"]] == ["later"]


def test_the_queue_drain_and_limit_watch_keep_the_prompt(h, monkeypatch):
    _no_provider(monkeypatch)  # the REAL helper: it refuses before launching
    monkeypatch.setattr(server, "_agent_activity", lambda inst, title: "offline")
    prompt_queue.enqueue("h", "go on")
    queue_driver._drain_one_queue("h")
    queue_driver._watch_one_limited("h")
    assert [i["text"] for i in prompt_queue.get_state("h")["items"]] == ["go on"]
    assert held.is_held("h")


def test_a_wake_refuses_a_held_session(h, monkeypatch):
    _no_provider(monkeypatch)
    sleep.mark("h", sleep.REASON_MANUAL)
    assert sleep.ensure_awake(h, "h", "test") == (False, HELD_REFUSED)
    assert held.is_held("h")


def test_the_sweep_skips_a_held_session(wt):
    srv = _Srv({"a": _Inst(wt), "b": _Inst(wt)})
    held.hold("a", held.REASON_RESTART)
    assert revive.skip_reason(srv, srv.ENGINE.instances["a"], "a", 0.0) == (
        revive.SKIP_HELD
    )
    out = _run(revive.sweep(srv, gap=0))
    assert out["revived"] == ["b"] and out["skipped"] == {"a": revive.SKIP_HELD}
    out = _run(revive.sweep(srv, gap=0, only=["a"]))  # Restart all without a Start
    assert out["revived"] == [] and held.is_held("a")


# --------------------------------------------------------------------------- #
# explicit Start releases the hold
# --------------------------------------------------------------------------- #
def test_revive_releases_the_hold_then_launches(h, monkeypatch):
    monkeypatch.setattr(server, "_ensure_agent_session", h.ensure)
    r = TestClient(server.app).post("/api/instances/h/revive")
    assert r.status_code == 200, r.text
    assert r.json()["revived"] is True and "memory_warning" in r.json()
    assert h.launched == ["h"] and not held.is_held("h")


def test_a_start_that_fails_keeps_the_session_held(h, monkeypatch):
    monkeypatch.setattr(
        server,
        "_ensure_agent_session",
        lambda i, t: ("mindflock_h", "workspace no longer exists"),
    )
    r = TestClient(server.app).post("/api/instances/h/revive")
    assert r.status_code == 409
    assert held.info("h")["reason"] == held.REASON_KILLED


# --------------------------------------------------------------------------- #
# an agent whose CLI isn't installed: Start says so instead of failing again
# --------------------------------------------------------------------------- #
def _no_cli(monkeypatch, found=None):
    """``shutil.which`` finds only the binaries in ``found``."""
    import shutil

    monkeypatch.setattr(
        shutil, "which", lambda b, *a, **k: b if b in (found or ()) else None
    )


@pytest.mark.parametrize("code", [127, 126])
def test_start_all_skips_a_session_whose_cli_is_not_installed(wt, monkeypatch, code):
    _no_cli(monkeypatch)
    srv = _Srv({"a": _Inst(wt, program="codex")}, markers={"a": code})
    held.hold("a", held.REASON_CRASH)
    out = _run(revive.sweep(srv, gap=0, release_held=True))
    assert out["skipped"] == {"a": revive.SKIP_MISSING}
    assert srv.launched == [] and held.is_held("a")


def test_once_installed_or_with_another_exit_start_all_launches_it(wt, monkeypatch):
    # Installed since: the stale 127 marker no longer refuses it.
    _no_cli(monkeypatch, found={"codex"})
    srv = _Srv({"a": _Inst(wt, program="codex")}, markers={"a": 127})
    held.hold("a", held.REASON_CRASH)
    assert _run(revive.sweep(srv, gap=0, release_held=True))["revived"] == ["a"]
    # Not installed, but it died some other way: a real crash, so try it.
    _no_cli(monkeypatch)
    srv = _Srv({"b": _Inst(wt, program="codex")}, markers={"b": 1})
    held.hold("b", held.REASON_CRASH)
    assert _run(revive.sweep(srv, gap=0, release_held=True))["revived"] == ["b"]


def test_start_on_a_missing_cli_says_what_is_wrong_and_stays_held(h, monkeypatch):
    _no_cli(monkeypatch)
    h.Program = "codex"
    monkeypatch.setattr(server, "_ensure_agent_session", h.ensure)
    monkeypatch.setattr(server, "_read_exit_marker", lambda name: 127)
    r = TestClient(server.app).post("/api/instances/h/revive")
    assert r.status_code == 409
    err = r.json()["error"]
    assert "Codex isn't installed" in err and "Settings → Agents" in err
    assert h.launched == [] and held.info("h")["reason"] == held.REASON_KILLED


@pytest.mark.parametrize("only", [None, ["a"]])
def test_start_all_releases_and_starts_held_sessions(wt, only):
    srv = _Srv({"a": _Inst(wt)})
    held.hold("a", held.REASON_RESTART)
    out = _run(revive.sweep(srv, gap=0, only=only, release_held=True))
    assert out["revived"] == ["a"] and srv.launched == ["a"]
    assert not held.is_held("a")


def test_start_all_keeps_the_hold_of_one_skipped_for_low_memory(wt, monkeypatch):
    monkeypatch.setattr(revive.memory_pressure, "is_low", lambda **kw: True)
    srv = _Srv({"a": _Inst(wt)})
    held.hold("a", held.REASON_RESTART)
    out = _run(revive.sweep(srv, gap=0, memory_wait=0, release_held=True))
    assert out["skipped"] == {"a": revive.SKIP_MEMORY}
    assert held.is_held("a")


@pytest.mark.parametrize("body", [None, {"titles": ["a", "b"]}])
def test_the_revive_all_route_is_a_start(monkeypatch, body):
    seen = {}

    async def _sweep(srv=None, **kw):
        seen.update(kw)
        return {"revived": [], "skipped": {}, "already_running": 0, "summary": ""}

    monkeypatch.setattr(server._revive, "sweep", _sweep)
    c = TestClient(server.app)
    r = (
        c.post("/api/server/revive-all", json=body)
        if body
        else c.post("/api/server/revive-all")
    )
    assert r.status_code == 200
    assert seen["release_held"] is True
    assert seen["only"] == (body or {}).get("titles")


def test_removing_or_pausing_drops_the_hold(h, monkeypatch):
    from backend.web.core import session_removal

    session_removal._forget_session_state("h")
    assert not held.is_held("h")
    held.hold("h", held.REASON_CRASH)
    session_removal._prune_session_state([])
    assert not held.is_held("h")
    held.hold("h", held.REASON_CRASH)
    monkeypatch.setattr(h, "Pause", lambda: setattr(h, "Status", Status.Paused))
    monkeypatch.setattr(server.ENGINE, "save", lambda *a, **k: None)
    r = TestClient(server.app).post("/api/instances/h/pause")
    assert r.status_code == 200, r.text
    assert not held.is_held("h")


# --------------------------------------------------------------------------- #
# the one boot notice
# --------------------------------------------------------------------------- #
def test_one_notice_after_the_boot_counts_the_held_sessions(wt, monkeypatch):
    srv = _Srv({"a": _Inst(wt), "b": _Inst(wt), "c": _Inst(wt)})
    held.hold("a", held.REASON_RESTART)
    held.hold("b", held.REASON_KILLED)
    seen = []
    monkeypatch.setattr(
        events.BUS, "emit", lambda event, **kw: seen.append((event, kw)) or kw
    )
    _run(revive.announce_held(srv))
    assert len(seen) == 1
    event, kw = seen[0]
    assert event == "system.sessions_held"
    assert kw["data"]["count"] == 2 and kw["data"]["titles"] == ["a", "b"]
    assert kw["data"]["detail"] == (
        "2 sessions stayed offline after the restart — press Start on the ones "
        "you need."
    )
    held.release("a")
    held.release("b")
    seen.clear()
    assert _run(revive.announce_held(srv)) is None and seen == []


def test_the_notice_rides_a_default_on_notify_rule():
    from backend.web.addons import notify

    assert "system.sessions_held" in events.EVENT_NAMES
    rule = next(r for r in notify.NOTIFY_RULES if r["id"] == "sessions_held")
    assert rule["event"] == "system.sessions_held"
    assert rule["default_enabled"] is True and rule["body"] == "{detail}"
    assert held.notice_text(1).startswith("1 session stayed offline")


@pytest.mark.parametrize("total,announced", [(2, True), (0, False)])
def test_the_lifespan_holds_before_serving_and_announces_once(
    monkeypatch, total, announced
):
    """Wiring only: the real hold reads ENGINE (the developer's live state),
    so it is faked here; its logic is pinned against fakes above."""
    calls = []
    monkeypatch.setattr(server, "_HOLD_ON_START_ENABLED", True)
    monkeypatch.setattr(
        revive, "hold_on_boot", lambda: calls.append("hold") or {"total": total}
    )

    async def _announce():
        return None

    monkeypatch.setattr(revive, "announce_held", _announce)
    registered = []

    def _register(coro):
        registered.append(getattr(coro, "__qualname__", ""))
        coro.close()

    monkeypatch.setattr(server, "_register_task", _register)
    with TestClient(server.app):
        assert calls == ["hold"]  # done before the first request is served
    assert any("_announce" in q for q in registered) is announced


def test_the_server_can_dump_its_stacks_on_sigusr1(tmp_path, monkeypatch):
    """A wedged server is read with `kill -USR1`, not "Restart WSL"."""
    import faulthandler
    import signal

    from backend import config
    from backend.web import run

    monkeypatch.setattr(config, "GetConfigDir", lambda: str(tmp_path))
    try:
        run._install_stack_dump()
        assert (tmp_path / "logs" / "stacks.log").exists()
    finally:
        faulthandler.unregister(signal.SIGUSR1)


def test_start_all_budgets_for_agents_still_warming_up(monkeypatch):
    """18 held sessions + Start all: each launch counts the agents started in
    the last WARMUP_SECONDS, so the sweep stops before they outgrow memory."""
    from backend import memory_pressure
    from backend.web.core import revive

    gib = 1024**3
    monkeypatch.setattr(memory_pressure, "is_low", lambda **k: False)
    monkeypatch.setattr(
        memory_pressure,
        "snapshot",
        lambda **k: {"available": 6 * gib, "total": 20 * gib},
    )
    monkeypatch.setattr(memory_pressure, "thresholds", lambda: {"low": (0.15, 0, 0, 0)})
    # 6 GiB free, 3 GiB is the low line: room for 4 warming agents (4 x 0.68), not 5.
    assert revive._headroom_ok(0)
    assert revive._headroom_ok(4)
    assert not revive._headroom_ok(5)
    monkeypatch.setattr(memory_pressure, "is_low", lambda **k: True)
    assert not revive._headroom_ok(0)
