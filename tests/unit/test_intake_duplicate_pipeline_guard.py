"""Two pipelines, one ticket, one cancelled route (2026-11-13).

Two servers shared ``~/.mindflock`` (:8765 from ``~``, :9000 from the app
checkout). Each started its own ticket pipeline, and the pipeline's singleton
lock sat in its cwd — so both ran. Pipeline A held sc-26087 for launch
spacing; meanwhile pipeline B launched it (session, worktree, flight). When
A's hold ended it re-armed onto B's flight (``in_flight`` → reused), failed
to create the session ("branch … is already checked out") and its failure
path cancelled the flight of B's LIVE session.

These pin the three fixes:

  1. **one pipeline per MindFlock home** — the lock lives in the home
     (``$MINDFLOCK_HOME``), the old per-root lock is still taken, and a
     server reads the other server's pipeline as running, not as its own
     stale child to restart;
  2. **a failed launch cancels only the flight it armed**, and a failure
     because the session is already live is "launched elsewhere": nothing
     cancelled, nothing recorded as failed — in the pipeline and in the
     server's forced starts;
  3. **re-check after the hold** — a launch that waited re-checks that no
     session of that title exists and that the ticket is still to be
     launched, before it arms anything.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.config import settings as store
from backend.ticket_ingestion import orchestrator as orchestrator_mod
from backend.ticket_ingestion import session_runner as sr
from backend.ticket_ingestion import singleton
from backend.ticket_ingestion.config import (
    EngineConfig,
    PipelineConfig,
    TicketProviderConfig,
)
from backend.ticket_ingestion.orchestrator import PipelineOrchestrator
from backend.ticket_ingestion.state import _read_state
from backend.web.addons import ticket_ingestion as ti
from backend.web.core import flights as F
from tests._factories import make_ticket

WILL_DO = "500000007"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINDFLOCK_WORKSPACE_DIR", str(tmp_path / "ws"))
    store.invalidate()
    yield
    store.invalidate()


# --------------------------------------------------------------------------- #
# 1. One pipeline per MindFlock home
# --------------------------------------------------------------------------- #
def _hold(path: Path, text: str):
    """Hold ``path``'s flock in this process (a second fd conflicts)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    fh = open(path, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def _release(fh) -> None:
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    fh.close()


@pytest.fixture
def pipeline_main(monkeypatch):
    from backend.ticket_ingestion import __main__ as main

    monkeypatch.setattr(main, "_LOCK_HANDLE", None)
    yield main
    for fh in main._LOCK_HANDLE or []:
        fh.close()


class TestOneLockPerHome:
    def test_the_lock_lives_in_the_mindflock_home(self, tmp_path):
        assert singleton.lock_path(tmp_path / "root") == (
            tmp_path / "home" / singleton.LOCK_NAME
        )

    def test_no_home_falls_back_to_the_per_root_lock(self, tmp_path, monkeypatch):
        """Under pytest the real home is refused: the old per-root lock."""
        monkeypatch.delenv("MINDFLOCK_HOME")
        assert singleton.lock_path(tmp_path) == tmp_path / singleton.LEGACY_LOCK_NAME

    def test_pipelines_in_two_roots_exclude_each_other(
        self, tmp_path, monkeypatch, pipeline_main
    ):
        """The incident: two servers start their pipelines from different
        cwds. With the lock in the home, the second one exits."""
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.chdir(a)
        assert pipeline_main._acquire_singleton_lock() is True
        first = pipeline_main._LOCK_HANDLE
        monkeypatch.setattr(pipeline_main, "_LOCK_HANDLE", None)
        try:
            monkeypatch.chdir(b)
            assert pipeline_main._acquire_singleton_lock() is False
            assert pipeline_main._LOCK_HANDLE is None
            # …and its exit line names the holder.
            assert "pid %d" % os.getpid() in pipeline_main._lock_holder_text()
        finally:
            for fh in first:
                fh.close()
        # Once the first is gone, the second may run.
        assert pipeline_main._acquire_singleton_lock() is True

    def test_an_older_pipeline_in_the_same_root_still_excludes(
        self, tmp_path, monkeypatch, pipeline_main
    ):
        """An older-build pipeline only holds the per-root lock: the new one
        must not run beside it — and must not keep the home lock it took."""
        monkeypatch.chdir(tmp_path)
        old = _hold(tmp_path / singleton.LEGACY_LOCK_NAME, "4242\n")
        try:
            assert pipeline_main._acquire_singleton_lock() is False
            assert singleton.probe(singleton.lock_path(tmp_path)) == (None, None)
        finally:
            _release(old)

    def test_the_lock_names_the_server_that_started_it(
        self, tmp_path, monkeypatch, pipeline_main
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(pipeline_main.OWNER_ENV, "server")
        monkeypatch.setenv(pipeline_main.SERVER_PID_ENV, "321")
        monkeypatch.setenv("MINDFLOCK_SERVER_PORT", "9000")
        assert pipeline_main._acquire_singleton_lock() is True
        for path in (
            singleton.lock_path(tmp_path),
            singleton.legacy_lock_path(tmp_path),
        ):
            lines = path.read_text().splitlines()
            assert int(lines[0]) == os.getpid()
            meta = json.loads(lines[1])
            assert meta["server_pid"] == 321 and meta["server_port"] == 9000
            assert meta["root"] == str(tmp_path) and meta["owner"] == "server"
        assert pipeline_main.SERVER_PID_ENV == ti.PIPELINE_SERVER_PID_ENV


@pytest.fixture
def ctrl(tmp_path):
    c = ti.TicketIngestionController()
    c._repo_root = tmp_path / "root"
    c._repo_root.mkdir()
    c._log_path = c._repo_root / "logs" / "ticket-ingestion.log"
    return c


def _meta(**kw) -> str:
    base = {"version": "0.1.0", "commit": "old", "owner": "server"}
    base.update(kw)
    return "777\n" + json.dumps(base) + "\n"


class TestServerReadsTheHomeLock:
    def test_the_controller_finds_a_pipeline_started_from_another_root(
        self, ctrl, tmp_path
    ):
        fh = _hold(ctrl._lock_path(), _meta(root=str(tmp_path)))
        try:
            assert ctrl._lock_path() == tmp_path / "home" / singleton.LOCK_NAME
            assert ctrl._external_lock_pid() == 777
            assert ctrl.is_running() is True
        finally:
            _release(fh)

    def test_an_older_pipeline_on_the_per_root_lock_is_still_found(self, ctrl):
        fh = _hold(ctrl._legacy_lock_path(), "555\n")
        try:
            assert ctrl._external_lock_pid() == 555
            # No metadata: an older build — restartable, as before.
            assert ctrl.stale_build() is True
        finally:
            _release(fh)

    def test_another_live_servers_pipeline_is_never_stale(self, ctrl, monkeypatch):
        """Two servers on different builds must not trade the pipeline back
        and forth: the one that didn't start it leaves it alone."""
        from backend.web.core import self_update

        monkeypatch.setattr(self_update, "installed_commit", lambda: "new")
        monkeypatch.setattr(self_update, "installed_version", lambda: "9.9.9")
        other = os.getppid()  # alive, and not this process
        fh = _hold(ctrl._lock_path(), _meta(server_pid=other, server_port=8765))
        try:
            assert ctrl.stale_build() is False
        finally:
            _release(fh)
        # The same lock from THIS server (re-exec'd onto an update): stale.
        fh = _hold(ctrl._lock_path(), _meta(server_pid=os.getpid()))
        try:
            assert ctrl.stale_build() is True
        finally:
            _release(fh)

    def test_an_orphan_whose_server_is_gone_is_restartable(self, ctrl, monkeypatch):
        from backend.web.core import self_update

        monkeypatch.setattr(self_update, "installed_commit", lambda: "new")
        monkeypatch.setattr(ti, "_pid_alive", lambda pid: False)
        fh = _hold(ctrl._lock_path(), _meta(server_pid=4242))
        try:
            assert ctrl.stale_build() is True
        finally:
            _release(fh)

    def test_the_losing_server_neither_restarts_nor_reports_an_error(
        self, ctrl, monkeypatch, tmp_path
    ):
        """Its child exited "already running" (exit 0): the other server's
        pipeline is the one running. start() no-ops (no restart loop), the
        status says whose it is, and the activity comes from ITS root."""
        other_root = tmp_path / "other-root"
        other_root.mkdir()
        (other_root / ".mindflock-pipeline-activity.json").write_text(
            json.dumps({"pid": 777, "ticket_busy": True})
        )
        monkeypatch.setattr(
            ti.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned a pipeline")
        )
        fh = _hold(
            ctrl._lock_path(),
            _meta(server_pid=os.getppid(), server_port=8765, root=str(other_root)),
        )
        try:
            assert ctrl.start() is False
            st = ctrl.status()
            assert st["running"] is True and st["pid"] == 777
            assert st["other_server"] == {"pid": os.getppid(), "port": 8765}
            assert st["exit_reason"] is None and st["last_error"] is None
            assert st["tickets_active"] is True
        finally:
            _release(fh)

    def test_reconcile_never_starts_beside_the_other_servers_pipeline(
        self, ctrl, monkeypatch
    ):
        addon = ti.TicketIngestionAddon()
        addon.ctrl = ctrl
        monkeypatch.setattr(
            ti.TicketIngestionAddon, "_process_wanted", staticmethod(lambda: True)
        )
        monkeypatch.setattr(ctrl, "start", lambda: pytest.fail("started"))
        monkeypatch.setattr(ctrl, "restart", lambda: pytest.fail("restarted"))
        fh = _hold(ctrl._lock_path(), _meta(server_pid=os.getppid()))
        try:
            addon._reconcile_process([])
        finally:
            _release(fh)

    def test_the_child_is_told_which_server_started_it(self, ctrl):
        assert ctrl._env()[ti.PIPELINE_SERVER_PID_ENV] == str(os.getpid())


# --------------------------------------------------------------------------- #
# The pipeline's launch (SessionRunner)
# --------------------------------------------------------------------------- #
def _settings_with_plan():
    path = store.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ticketing": {
                    "sources": [
                        {"id": "sc", "provider": "shortcut", "plan": "builtin:ticket"}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    store.invalidate()


def _runner(tmp_path, monkeypatch):
    cfg = PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut", api_token="t", member_id="m"
        ),
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "workspaces",
        min_description_length=20,
        log_file=tmp_path / "pipeline.log",
        log_level="INFO",
        engine=EngineConfig(enabled=True, mode="worktree"),
    )
    runner = sr.SessionRunner(cfg)
    monkeypatch.setattr(runner, "_arm_autopilot", lambda *a: None)
    return runner


def _story():
    story = make_ticket(
        id=42,
        name="Fix the flux",
        description="A" * 50,
        acceptance_criteria=["works"],
        owner_ids=["u1"],
        app_url="https://app.shortcut.com/org/story/42",
        created_at=datetime(2026, 1, 1),
    )
    story.source_key = "sc"
    return story


def _held(monkeypatch, seconds: float) -> None:
    async def _hold_for(what):
        return seconds

    monkeypatch.setattr(sr, "_wait_for_launch_room", _hold_for)


class TestOnlyItsOwnFlight:
    def test_a_reused_flight_survives_a_failed_create(self, tmp_path, monkeypatch):
        """The incident's cancel: another launch armed the flight, this one
        reused it and failed — the flight is not this launch's to cancel."""
        runner = _runner(tmp_path, monkeypatch)
        _settings_with_plan()
        theirs = F.arm_pending("sc-42", "builtin:ticket", {"kind": "ticket"})
        with patch.object(
            runner,
            "_create_instance",
            side_effect=RuntimeError("branch sc-42 is already checked out at /wt"),
        ):
            with pytest.raises(RuntimeError):
                asyncio.run(runner.run(_story()))
        f = F.for_session("sc-42")
        assert f["id"] == theirs["id"] and f["state"] != "cancelled"

    def test_a_flight_this_launch_armed_is_still_cancelled(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        _settings_with_plan()
        with patch.object(runner, "_create_instance", side_effect=RuntimeError("x")):
            with pytest.raises(RuntimeError):
                asyncio.run(runner.run(_story()))
        (f,) = F.list_flights()
        assert f["state"] == "cancelled"

    def test_arm_reports_what_it_took_off_not_what_it_reused(
        self, tmp_path, monkeypatch
    ):
        runner = _runner(tmp_path, monkeypatch)
        _settings_with_plan()
        subject = {"kind": "ticket", "source": "sc", "id": "42"}
        armed: list = []
        runner._arm("sc-42", "tix", "sc", "42", "x", subject=subject, armed=armed)
        assert armed == [F.for_session("sc-42")["id"]]
        again: list = []
        runner._arm("sc-42", "tix", "sc", "42", "x", subject=subject, armed=again)
        assert again == []

    def test_failing_because_the_session_is_live_is_launched_elsewhere(
        self, tmp_path, monkeypatch, caplog
    ):
        """The session appeared while this launch was creating its own: not a
        failure, and even the flight THIS launch armed stays (the live
        session flies it)."""
        runner = _runner(tmp_path, monkeypatch)
        _settings_with_plan()
        live: set = set()

        def _create(*a, **kw):
            live.add("sc-42")
            raise RuntimeError("branch sc-42 is already checked out at /wt")

        monkeypatch.setattr(runner, "_stored_titles", lambda: set(live))
        with patch.object(runner, "_create_instance", side_effect=_create):
            with caplog.at_level(logging.INFO, logger=sr.logger.name):
                with pytest.raises(sr.NotLaunched) as exc:
                    asyncio.run(runner.run(_story()))
        assert exc.value.launched_elsewhere is True
        assert F.for_session("sc-42")["state"] != "cancelled"
        assert "launched elsewhere" in caplog.text


class TestRecheckAfterTheHold:
    def test_a_live_session_of_that_title_stops_it_before_arming(
        self, tmp_path, monkeypatch
    ):
        runner = _runner(tmp_path, monkeypatch)
        _settings_with_plan()
        _held(monkeypatch, 55.0)
        monkeypatch.setattr(runner, "_stored_titles", lambda: {"sc-42"})
        with patch.object(runner, "_create_instance") as create:
            with pytest.raises(sr.NotLaunched) as exc:
                asyncio.run(runner.run(_story()))
        create.assert_not_called()
        assert exc.value.launched_elsewhere is True
        assert F.list_flights() == []

    def test_a_ticket_that_left_its_state_during_the_hold_is_dropped(
        self, tmp_path, monkeypatch
    ):
        runner = _runner(tmp_path, monkeypatch)
        _settings_with_plan()
        _held(monkeypatch, 55.0)
        recheck = AsyncMock(return_value="it's now in In Progress since it was queued")
        with patch.object(runner, "_create_instance") as create:
            with pytest.raises(sr.NotLaunched) as exc:
                asyncio.run(runner.run(_story(), recheck=recheck))
        create.assert_not_called()
        assert exc.value.launched_elsewhere is False
        assert "In Progress" in exc.value.reason
        assert F.list_flights() == []

    def test_no_hold_no_second_read(self, tmp_path, monkeypatch):
        """The caller's checks ran a moment ago: no extra tracker call."""
        runner = _runner(tmp_path, monkeypatch)
        _held(monkeypatch, 0.0)
        recheck = AsyncMock(return_value="should not be asked")
        with patch.object(
            runner,
            "_create_instance",
            return_value=MagicMock(GetWorktreePath=lambda: None),
        ):
            asyncio.run(runner.run(_story(), recheck=recheck))
        recheck.assert_not_awaited()

    def test_a_recheck_that_errors_launches_as_vetted(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        _held(monkeypatch, 55.0)
        recheck = AsyncMock(side_effect=RuntimeError("tracker down"))
        with patch.object(
            runner,
            "_create_instance",
            return_value=MagicMock(GetWorktreePath=lambda: None),
        ) as create:
            asyncio.run(runner.run(_story(), recheck=recheck))
        create.assert_called_once()

    def test_a_pr_with_a_live_session_is_not_provisioned(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion.models import PullRequest

        runner = _runner(tmp_path, monkeypatch)
        _held(monkeypatch, 55.0)
        pr = PullRequest(
            number=7,
            head_ref="feature/x",
            head_sha="deadbeef",
            base_ref="main",
            title="t",
            url="https://github.com/org/repo/pull/7",
            author="a",
            created_at=datetime(2026, 1, 2),
            repo="org/repo",
        )
        monkeypatch.setattr(runner, "hand_to_builder", AsyncMock(return_value=""))
        from backend.ticket_ingestion.models import pr_slug

        monkeypatch.setattr(runner, "_stored_titles", lambda: {"pr-%s" % pr_slug(pr)})
        runner._pr_provisioner.provision = AsyncMock()
        with pytest.raises(sr.NotLaunched):
            asyncio.run(runner.run_pr(pr, []))
        runner._pr_provisioner.provision.assert_not_awaited()


# --------------------------------------------------------------------------- #
# The orchestrator: a back-off is not a failure
# --------------------------------------------------------------------------- #
@pytest.fixture
def ledger(tmp_path, monkeypatch):
    d = tmp_path / "ledger"
    d.mkdir()
    monkeypatch.setattr(orchestrator_mod, "_STATE_DIR", d)
    monkeypatch.setattr(orchestrator_mod, "_fleet_holder", lambda *a: None)
    return d


def _orchestrator(tmp_path) -> PipelineOrchestrator:
    source = TicketProviderConfig(
        provider="shortcut",
        id="sc",
        api_token="t",
        member_id="member-123",
        workflow_state=WILL_DO,
    )
    orch = PipelineOrchestrator(
        PipelineConfig(
            ticketing=source,
            ticketing_sources=[source],
            repo_url="git@github.com:org/repo.git",
            workspace_dir=tmp_path / "workspaces",
            min_description_length=20,
            log_file=tmp_path / "pipeline.log",
        )
    )
    orch._ingest_filter_miss_now = AsyncMock(return_value="")
    orch._tracker_claim_now = AsyncMock(return_value=None)
    orch._cs_runner = MagicMock()
    return orch


def _queued():
    return make_ticket(
        id=7,
        source_key="sc",
        state_id=WILL_DO,
        description="A queued ticket with a long enough description.",
        owner_ids=["member-123"],
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class TestOrchestratorBackOff:
    async def test_launched_elsewhere_is_recorded_skipped_not_failed(
        self, tmp_path, ledger
    ):
        orch = _orchestrator(tmp_path)
        orch._cs_runner.run = AsyncMock(
            side_effect=sr.NotLaunched("already live", launched_elsewhere=True)
        )
        await orch.process_story(_queued())
        (entry,) = _read_state(ledger)["processed_stories"]
        assert entry["status"] == "skipped"
        assert entry["failure_reason"] == "already live"

    async def test_no_longer_wanted_records_nothing(self, tmp_path, ledger, caplog):
        orch = _orchestrator(tmp_path)
        orch._cs_runner.run = AsyncMock(side_effect=sr.NotLaunched("moved away"))
        with caplog.at_level(logging.INFO, logger=orchestrator_mod.__name__):
            await orch.process_story(_queued())
        assert _read_state(ledger).get("processed_stories", []) == []
        assert "Not launching sc-7: moved away." in caplog.text

    async def test_the_runner_is_handed_the_same_checks_to_ask_again(
        self, tmp_path, ledger
    ):
        """The pre-launch checks pass at dequeue; asked again (after the hold)
        the ticket has moved — the recheck says why."""
        orch = _orchestrator(tmp_path)
        orch._ingest_filter_miss_now = AsyncMock(side_effect=["", "it's now in Done"])
        answers = []

        async def _run(story, supplemental_context=None, recheck=None):
            answers.append(await recheck())
            raise sr.NotLaunched(answers[-1])

        orch._cs_runner.run = _run
        await orch.process_story(_queued())
        assert answers == ["it's now in Done since it was queued"]
        assert _read_state(ledger).get("processed_stories", []) == []

    async def test_the_recheck_asks_the_tracker_claim_too(self, tmp_path, ledger):
        orch = _orchestrator(tmp_path)
        orch._tracker_claim_now = AsyncMock(
            side_effect=[None, {"who": "Ann", "how": "assigned"}]
        )
        seen = []

        async def _run(story, supplemental_context=None, recheck=None):
            seen.append(await recheck())
            return "sc-7"

        orch._cs_runner.run = _run
        with patch(
            "backend.ticket_ingestion.tracker_claims.describe",
            lambda c: "claimed by %s" % c["who"],
        ):
            await orch.process_story(_queued())
        assert seen == ["claimed by Ann"]


# --------------------------------------------------------------------------- #
# The server's forced starts (ticket / issue / PR review)
# --------------------------------------------------------------------------- #
class TestServerStarts:
    def test_cancel_leaves_a_flight_another_start_armed(self, monkeypatch):
        from backend.web import server

        monkeypatch.setattr(server.ENGINE, "instances", {})
        theirs = F.arm_pending("sc-9", "builtin:ticket", {"kind": "ticket"})
        server._cancel_intake_flight("sc-9", "could not be created", "")
        assert F.for_session("sc-9")["state"] != "cancelled"
        server._cancel_intake_flight("sc-9", "could not be created", "f_other")
        assert F.for_session("sc-9")["state"] != "cancelled"
        server._cancel_intake_flight("sc-9", "could not be created", theirs["id"])
        assert F.get(theirs["id"])["state"] == "cancelled"

    def test_arm_intake_reports_the_flight_it_armed(self, monkeypatch):
        from backend.web import server

        monkeypatch.setattr(server.ENGINE, "instances", {})
        monkeypatch.setattr(server, "_arm_intake_autopilot", lambda *a, **k: None)
        armed: list = []
        server._arm_intake(
            "sc-10",
            "",
            "tix",
            "10",
            plan="builtin:ticket",
            subject={"kind": "ticket"},
            armed=armed,
        )
        assert armed == [F.for_session("sc-10")["id"]]

    def test_launched_elsewhere_is_another_live_instance(self):
        from backend.web.core.ticket_start import launched_elsewhere

        ours, theirs = object(), object()
        srv = SimpleNamespace(
            ENGINE=SimpleNamespace(instances={"sc-1": theirs}),
            log=SimpleNamespace(InfoLog=None),
        )
        assert launched_elsewhere(srv, "sc-1", ours, RuntimeError("x")) is True
        assert launched_elsewhere(srv, "sc-1", None, RuntimeError("x")) is True
        assert launched_elsewhere(srv, "sc-1", theirs, RuntimeError("x")) is False
        assert launched_elsewhere(srv, "sc-2", ours, RuntimeError("x")) is False
