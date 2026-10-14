"""Background revive (``backend.web.core.revive``) and "nothing disappears
silently" (sessions the engine drops at load land in Recently closed).

The sweep runs against a fake server namespace — fake instances, a fake
``_ensure_agent_session`` — so nothing here touches tmux or launches an agent.
Pins: who is skipped and why (paused / provisioning / peer-shared / workspace
missing / usage limit / already running), oldest-first order, the cap of two
launches in flight, the summary line, the ``general.revive_on_start`` setting
(default on), ``POST /api/server/revive-all`` and its 403, and the load-time
Recently closed record with ``reason`` + ``branch_exists``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.session.storage import (
    GitWorktreeData,
    InstanceData,
    Status,
    _marshal_instances,
)
from backend.web.core import engine as engine_mod
from backend.web.core import recently_closed as rc
from backend.web.core import revive


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
def _dt_ago(seconds: float) -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds)


class _Inst:
    def __init__(
        self, wt, *, status=Status.Running, age=100.0, peer="", program="claude"
    ):
        self.Status = status
        self.PeerShare = peer
        self.Program = program
        self._wt = wt
        self.CreatedAt = _dt_ago(age)
        self.UpdatedAt = _dt_ago(age)

    def GetWorktreePath(self):
        return self._wt


class _Srv(SimpleNamespace):
    """The slice of ``backend.web.server`` the sweep uses."""

    def __init__(self, instances, *, limited=(), alive=(), delay=0.0, fail=None):
        super().__init__()
        self.ENGINE = SimpleNamespace(instances=instances, lock=threading.RLock())
        self._limited = set(limited)
        self._alive = set(alive)
        self._delay = delay
        self._fail = fail or {}
        self.launched = []
        self.republished = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._mu = threading.Lock()
        self._BOOT_QUIET_SECONDS = 0.0
        self._BOOT_MONO = time.monotonic()

    def _session_limited_until(self, title):
        return time.time() + 3600 if title in self._limited else 0.0

    def _live_limit_reset(self, provider, now, inst=None):
        return None  # no meter reading

    def _live_session_name(self, name):
        return name if any(name.endswith(t) for t in self._alive) else None

    def _ensure_agent_session(self, inst, title):
        with self._mu:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        time.sleep(self._delay)
        with self._mu:
            self.in_flight -= 1
            self.launched.append(title)
        return "mindflock_" + title, self._fail.get(title)

    def _republish_session(self, title):
        self.republished.append(title)


@pytest.fixture
def wt(tmp_path):
    d = tmp_path / "wt"
    d.mkdir()
    return str(d)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# memory: the sweep waits for headroom, then holds the rest
# --------------------------------------------------------------------------- #
def test_low_memory_holds_the_sweep_instead_of_relaunching_into_it(wt, monkeypatch):
    """Relaunching everything straight after the OOM killer took it down is
    how it happens again: with no headroom coming, nothing launches and every
    title says why."""
    monkeypatch.setattr(revive.memory_pressure, "is_low", lambda **kw: True)
    srv = _Srv({"a": _Inst(wt), "b": _Inst(wt)})
    out = _run(revive.sweep(srv, gap=0, memory_wait=0))
    assert srv.launched == []
    assert out["skipped"] == {"a": revive.SKIP_MEMORY, "b": revive.SKIP_MEMORY}
    assert "2 low memory" in out["summary"]


def test_the_sweep_goes_on_once_memory_recovers(wt, monkeypatch):
    readings = iter([True, True, False])
    monkeypatch.setattr(
        revive.memory_pressure, "is_low", lambda **kw: next(readings, False)
    )
    monkeypatch.setattr(revive, "MEMORY_POLL_SECONDS", 0.0)
    srv = _Srv({"a": _Inst(wt)})
    out = _run(revive.sweep(srv, gap=0, memory_wait=5))
    assert out["revived"] == ["a"] and srv.launched == ["a"]


# --------------------------------------------------------------------------- #
# continue: an agent that died mid-turn is told to carry on
# --------------------------------------------------------------------------- #
class _ContSrv(_Srv):
    """Adds the exit marker + queue-changed hooks :func:`revive.relaunch`
    reads; ``codes`` maps title → the launch wrapper's recorded exit code."""

    def __init__(self, instances, codes=None, **kw):
        super().__init__(instances, **kw)
        self._codes = codes or {}
        self.queue_events = []

    def _read_exit_marker(self, name):
        return self._codes.get(name.replace("mindflock_", "", 1))

    def _is_natural_exit(self, code):
        return code in (0, 130)

    def _emit_queue_changed(self, title):
        self.queue_events.append(title)


def _mark(title, state, age=5.0):
    from backend.providers import activity_markers as am
    from backend.session import tmux

    path = am.marker_path(tmux.to_mindflock_tmux_name(title))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": state, "ts": time.time() - age}))


def _queued(title):
    from backend.web.core import prompt_queue

    return [it["text"] for it in prompt_queue.list_queue(title)]


def test_a_kill_mid_turn_queues_a_continue_at_the_front(wt):
    from backend.web.core import prompt_queue

    prompt_queue.enqueue("k", "the next task")
    _mark("k", "working")
    srv = _ContSrv({"k": _Inst(wt)}, codes={"k": 137})
    name, err, continued = revive.relaunch(srv.ENGINE.instances["k"], "k", srv)
    assert err is None and continued is True
    assert _queued("k") == [revive.CONTINUE_PROMPT, "the next task"]
    assert srv.queue_events == ["k"]


@pytest.mark.parametrize(
    "state, code, why",
    [
        ("idle", 137, "the turn had ended (Stop hook)"),
        ("clarify", None, "it was waiting on a person, not working"),
        ("working", 0, "a clean quit starts fresh: nothing to continue"),
        ("working", 130, "Ctrl+C is a quit too"),
    ],
)
def test_no_continue_unless_it_died_mid_turn(wt, state, code, why):
    _mark("n", state)
    srv = _ContSrv({"n": _Inst(wt)}, codes={"n": code})
    _, err, continued = revive.relaunch(srv.ENGINE.instances["n"], "n", srv)
    assert err is None
    assert continued is False, why
    assert _queued("n") == []


def test_a_crash_without_an_exit_marker_still_counts(wt):
    """SIGKILL of the whole tmux (the OOM killer's usual target) leaves no
    exit marker at all: that is unnatural, so it resumes and continues."""
    _mark("c", "working")
    srv = _ContSrv({"c": _Inst(wt)})
    assert revive.died_mid_turn(srv, srv.ENGINE.instances["c"], "c") is True


def test_a_stale_working_marker_is_not_a_turn(wt):
    """Over 6h old: whatever it was doing is long gone; don't prod it."""
    _mark("s", "working", age=7 * 3600)
    srv = _ContSrv({"s": _Inst(wt)})
    assert revive.died_mid_turn(srv, srv.ENGINE.instances["s"], "s") is False


def test_a_live_agent_or_a_cli_that_cannot_resume_gets_no_continue(wt):
    _mark("live", "working")
    srv = _ContSrv({"live": _Inst(wt)}, alive={"live"})
    assert revive.died_mid_turn(srv, srv.ENGINE.instances["live"], "live") is False
    # cline has no resume flag: its relaunch is a fresh conversation, which
    # "continue where you left off" would only confuse.
    _mark("cl", "working")
    srv = _ContSrv({"cl": _Inst(wt, program="cline")})
    assert revive.died_mid_turn(srv, srv.ENGINE.instances["cl"], "cl") is False


def test_a_paused_queue_is_not_switched_back_on(wt):
    """Enqueueing switches a queue on, which would release the prompts the
    user paused: leave it alone."""
    from backend.web.core import prompt_queue

    prompt_queue.enqueue("p", "held task")
    prompt_queue.set_flags("p", enabled=False)
    _mark("p", "working")
    srv = _ContSrv({"p": _Inst(wt)}, codes={"p": 137})
    _, _, continued = revive.relaunch(srv.ENGINE.instances["p"], "p", srv)
    assert continued is False
    assert _queued("p") == ["held task"]
    assert prompt_queue.get_state("p")["enabled"] is False


def test_the_sweep_reports_who_was_told_to_continue(wt):
    _mark("mid", "working")
    _mark("done", "idle")
    srv = _ContSrv({"mid": _Inst(wt), "done": _Inst(wt)})
    out = _run(revive.sweep(srv, gap=0))
    assert sorted(out["revived"]) == ["done", "mid"]
    assert out["continued"] == ["mid"]
    assert out["summary"].endswith("; told 1 to continue")


def test_restart_all_sweeps_only_the_named_rows_quit_ones_included(wt):
    """The rail's "Restart all" names the rows whose chip offers Restart — an
    agent the user quit included — and nothing else (a hidden session's dead
    agent stays dead)."""
    srv = _ContSrv(
        {"quit": _Inst(wt), "crashed": _Inst(wt), "hidden": _Inst(wt)},
        codes={"quit": 0},
    )
    plain = revive.plan(srv)
    assert plain[1] == {"quit": revive.SKIP_QUIT}
    out = _run(revive.sweep(srv, gap=0, only=["quit", "crashed"]))
    assert sorted(out["revived"]) == ["crashed", "quit"]
    assert "hidden" not in srv.launched


# --------------------------------------------------------------------------- #
# the sweep
# --------------------------------------------------------------------------- #
def test_sweep_skips_with_reasons_and_revives_the_rest(wt, tmp_path):
    srv = _Srv(
        {
            "run": _Inst(wt),
            "ready": _Inst(wt, status=Status.Ready),
            "paused": _Inst(wt, status=Status.Paused),
            "loading": _Inst(wt, status=Status.Loading),
            "peer": _Inst(wt, peer="share-1"),
            "missing": _Inst(str(tmp_path / "gone")),
            "limit": _Inst(wt),
            "alive": _Inst(wt),
        },
        limited={"limit"},
        alive={"alive"},
    )
    out = _run(revive.sweep(srv, gap=0))
    assert sorted(out["revived"]) == ["ready", "run"]
    assert out["skipped"] == {
        "paused": revive.SKIP_PAUSED,
        "loading": revive.SKIP_LOADING,
        "peer": revive.SKIP_PEER,
        "missing": revive.SKIP_WORKSPACE,
        "limit": revive.SKIP_LIMIT,
    }
    assert out["already_running"] == 1
    assert sorted(srv.launched) == ["ready", "run"]
    # Each landed revive republishes its row (status/activity events fire).
    assert sorted(srv.republished) == ["ready", "run"]
    assert out["summary"] == (
        "revived 2, skipped 5: 1 paused, 1 peer-shared, 1 still provisioning, "
        "1 usage limit, 1 workspace missing; 1 already running"
    )


def test_sweep_is_oldest_first_and_caps_two_in_flight(wt):
    srv = _Srv(
        {
            "new": _Inst(wt, age=10),
            "old": _Inst(wt, age=5000),
            "mid": _Inst(wt, age=500),
            "older": _Inst(wt, age=9000),
            "mid2": _Inst(wt, age=600),
        },
        delay=0.05,
    )
    out = _run(revive.sweep(srv, gap=0))
    assert len(out["revived"]) == 5
    assert srv.max_in_flight == 2
    order, skipped, alive = revive.plan(srv)
    assert order == ["older", "old", "mid2", "mid", "new"]
    assert skipped == {} and alive == 0


def test_sweep_concurrency_one_runs_strictly_in_order(wt):
    srv = _Srv({"b": _Inst(wt, age=10), "a": _Inst(wt, age=20)})
    out = _run(revive.sweep(srv, concurrency=1, gap=0))
    assert srv.launched == ["a", "b"]
    assert out["revived"] == ["a", "b"]


def test_a_failed_launch_is_reported_not_raised(wt):
    srv = _Srv({"x": _Inst(wt)}, fail={"x": "workspace no longer exists"})
    out = _run(revive.sweep(srv, gap=0))
    assert out["revived"] == []
    assert out["skipped"] == {"x": "workspace no longer exists"}
    assert srv.republished == []


def test_a_session_paused_after_the_plan_is_not_launched(wt):
    inst = _Inst(wt)
    srv = _Srv({"x": inst})
    todo, _, _ = revive.plan(srv)
    assert todo == ["x"]
    inst.Status = Status.Paused
    assert revive._revive_one(srv, "x") == (False, revive.SKIP_PAUSED)
    assert srv.launched == []


def test_nothing_to_do_summary():
    out = _run(revive.sweep(_Srv({}), gap=0))
    assert out == {
        "revived": [],
        "continued": [],
        "skipped": {},
        "already_running": 0,
        "summary": "revived 0, skipped 0",
    }


def test_live_meter_limit_skips(wt, monkeypatch):
    srv = _Srv({"x": _Inst(wt)})
    monkeypatch.setattr(
        srv, "_live_limit_reset", lambda p, now, inst=None: now + 600, raising=False
    )
    assert revive.skip_reason(srv, srv.ENGINE.instances["x"], "x", time.time()) == (
        revive.SKIP_LIMIT
    )


# --------------------------------------------------------------------------- #
# the setting + startup gate
# --------------------------------------------------------------------------- #
def test_revive_on_start_defaults_on_and_round_trips():
    assert S.load_settings().general.revive_on_start is None
    assert revive.enabled() is True
    S.update_settings(general={"revive_on_start": False})
    S.invalidate()
    assert S.load_settings().general.revive_on_start is False
    assert revive.enabled() is False
    S.update_settings(general={"revive_on_start": True})
    assert revive.enabled() is True


def test_run_on_start_respects_the_setting(wt):
    srv = _Srv({"x": _Inst(wt)})
    S.update_settings(general={"revive_on_start": False})
    assert _run(revive.run_on_start(srv)) is None
    assert srv.launched == []
    S.update_settings(general={"revive_on_start": True})
    out = _run(revive.run_on_start(srv))
    assert out["revived"] == ["x"]


def test_run_on_start_waits_out_the_boot_quiet_window(wt, monkeypatch):
    srv = _Srv({})
    srv._BOOT_QUIET_SECONDS = 30.0
    srv._BOOT_MONO = time.monotonic() - 29.5
    slept = []

    async def _sleep(s):
        slept.append(s)

    monkeypatch.setattr(revive.asyncio, "sleep", _sleep)
    _run(revive.run_on_start(srv))
    assert slept and 0.5 < slept[0] <= 2.0


def test_startup_sweep_is_off_under_pytest():
    from backend.web import server

    assert server._revive_on_start_loop_enabled() is False


# --------------------------------------------------------------------------- #
# POST /api/server/revive-all
# --------------------------------------------------------------------------- #
def test_revive_all_route_returns_the_summary(monkeypatch):
    from backend.web import server

    seen = {}

    async def _sweep(srv=None, **kw):
        seen.update(kw)
        return {"revived": ["a"], "skipped": {}, "already_running": 0, "summary": "s"}

    monkeypatch.setattr(server._revive, "sweep", _sweep)
    r = TestClient(server.app).post("/api/server/revive-all")
    assert r.status_code == 200
    assert r.json()["revived"] == ["a"]
    assert seen.get("trigger") == "on demand"


def test_revive_all_route_is_owner_only(monkeypatch):
    from backend.web import server

    async def _no(scope):
        return False

    called = []

    async def _sweep(*a, **kw):
        called.append(1)
        return {}

    monkeypatch.setattr(server._auth, "may_configure", _no)
    monkeypatch.setattr(server._revive, "sweep", _sweep)
    r = TestClient(server.app).post("/api/server/revive-all")
    assert r.status_code == 403
    assert called == []


# --------------------------------------------------------------------------- #
# nothing disappears silently: the load-time Recently closed record
# --------------------------------------------------------------------------- #
@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(engine_mod, "_tmux_session_exists", lambda title: False)
    return h


def _git_repo(path, branch):
    path.mkdir()
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(path), *a], check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    run("branch", branch)


def _dead(title, repo, branch, wt):
    t = _dt_ago(600)
    return InstanceData(
        title=title,
        path=str(repo),
        branch=branch,
        status=Status.Running,
        created_at=t,
        updated_at=t,
        program="bash",
        worktree=GitWorktreeData(
            repo_path=str(repo),
            worktree_path=str(wt),
            session_name=title,
            branch_name=branch,
        ),
    )


def test_load_records_a_dropped_session_in_recently_closed(home, tmp_path):
    from backend import config

    repo = tmp_path / "repo"
    _git_repo(repo, "feature/kept")
    st = config.LoadState()
    st.SaveInstances(
        _marshal_instances(
            [
                _dead("kept-branch", repo, "feature/kept", tmp_path / "gone1"),
                _dead("lost-branch", repo, "feature/lost", tmp_path / "gone2"),
            ]
        )
    )
    eng = engine_mod.Engine()
    assert "kept-branch" not in eng.instances
    assert "lost-branch" not in eng.instances
    entries = json.loads((home / ".mindflock" / "recently_closed.json").read_text())
    by_title = {e["title"]: e for e in entries}
    kept, lost = by_title["kept-branch"], by_title["lost-branch"]
    for e in (kept, lost):
        assert e["reason"] == "workspace missing"
        assert e["wiped"] is True
    assert kept["branch_exists"] is True and kept["branch"] == "feature/kept"
    assert lost["branch_exists"] is False

    # A second load (state.json not yet rewritten) does not pile up duplicates.
    engine_mod.Engine()
    again = json.loads((home / ".mindflock" / "recently_closed.json").read_text())
    assert sorted(e["title"] for e in again) == ["kept-branch", "lost-branch"]


def test_the_list_routes_carry_reason_and_branch_exists(home, tmp_path, monkeypatch):
    from backend.web import server

    repo = tmp_path / "repo"
    _git_repo(repo, "feature/kept")
    rc.record_missing_workspace(_dead("gone", repo, "feature/kept", tmp_path / "x"))
    c = TestClient(server.app)
    row = next(e for e in c.get("/api/recently-closed").json() if e["title"] == "gone")
    assert row["reason"] == "workspace missing"
    assert row["branch_exists"] is True and row["wiped"] is True and not row["exists"]
    monkeypatch.setattr(server, "_active_worktree_titles", lambda: {})
    rows = c.get("/api/recent").json()["rows"]
    row = next(e for e in rows if e.get("title") == "gone")
    assert row["reason"] == "workspace missing" and row["branch_exists"] is True
    # Not reopenable: the folder is gone.
    assert c.post("/api/recently-closed/%s/reopen" % row["id"]).status_code == 410


def test_branch_exists_is_false_without_a_repo(tmp_path):
    assert rc._branch_exists("", "b") is False
    assert rc._branch_exists(str(tmp_path / "nope"), "b") is False
