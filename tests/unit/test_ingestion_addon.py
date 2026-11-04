"""Unit tests for the Ticket Ingestion addon (backend.web.addons.ticket_ingestion).

Covers the pure decision surface with no other coverage: the six settings-backed
feature gates, ``_process_wanted``/``_reconcile_process`` toggle reconciliation,
the log-tail connectivity-error detector, the activity-beacon parser, the
status() assembly, the singleton-lock probe, and the start-gate on the ``/start``
endpoint. The real subprocess lifecycle (``start``/``stop``/``restart`` spawning
``python -m backend.ticket_ingestion``) and the ``/logs`` tail websocket are left
uncovered — they spawn a real detached process group / need a PTY.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web.addons import ticket_ingestion as ti


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Every gate reads the user settings store; keep it per-test isolated."""


@pytest.fixture
def ctrl(tmp_path):
    """A controller with its repo root / log path pinned under tmp_path so no
    test touches the real repo or spawns anything."""
    c = ti.TicketIngestionController()
    c._repo_root = tmp_path
    c._log_path = tmp_path / "logs" / "ticket-ingestion.log"
    return c


# --------------------------------------------------------------------------- #
# Feature gates
# --------------------------------------------------------------------------- #
class TestDesiredRunning:
    def test_default_false(self):
        assert ti._desired_running() is False

    def test_reflects_setting(self):
        S.update_settings(general={"ingestion_autostart": True})
        assert ti._desired_running() is True

    def test_record_persists_and_is_idempotent(self, monkeypatch):
        writes = []
        real_update = S.update_settings

        def spy(**kw):
            writes.append(kw)
            return real_update(**kw)

        monkeypatch.setattr(S, "update_settings", spy)
        ti._record_desired_running(True)
        assert S.load_settings().general.ingestion_autostart is True
        assert len(writes) == 1
        ti._record_desired_running(True)  # unchanged -> no second write
        assert len(writes) == 1


class TestPrReviewEnabled:
    def test_off_without_repos(self):
        S.update_settings(github={"enabled": True})
        assert ti._pr_review_enabled() is False

    def test_on_when_repos_and_unset_enabled(self):
        # enabled unset counts as on (matches the UI), so repos alone flip it on.
        S.update_settings(github={"repos": ["o/r"]})
        assert ti._pr_review_enabled() is True

    def test_off_when_explicitly_disabled(self):
        S.update_settings(github={"repos": ["o/r"], "enabled": False})
        assert ti._pr_review_enabled() is False


class TestIssueHandlingEnabled:
    def test_off_when_unset(self):
        # issues_enabled is opt-in: unset counts as OFF even with repos.
        S.update_settings(github={"issue_repos": ["o/r"]})
        assert ti._issue_handling_enabled() is False

    def test_on_when_enabled_and_repos(self):
        S.update_settings(github={"issues_enabled": True, "issue_repos": ["o/r"]})
        assert ti._issue_handling_enabled() is True

    def test_off_when_enabled_but_no_repos(self):
        S.update_settings(github={"issues_enabled": True})
        assert ti._issue_handling_enabled() is False


class TestAutomationHere:
    """PR review / issue handling settings follow the person to every one of
    their devices; ONE device of the group runs them — the synced
    ``github.automation_device``, else the lowest-keyed live member — so they
    don't all review the same PRs. A lone device always does."""

    @staticmethod
    def _fleet(monkeypatch, n, me="d1"):
        """A group of ``n`` (d0…); this device is ``me`` — by default not
        the lowest key, so not the runner unless chosen."""
        from backend.web.core import fleet, remote

        monkeypatch.setattr(fleet, "in_fleet", lambda: n > 0)
        monkeypatch.setattr(
            fleet, "live_members", lambda: {"d%d" % i: {} for i in range(n)}
        )
        monkeypatch.setattr(remote, "self_identity", lambda: {"key": me, "host": me})

    def test_a_lone_device_behaves_as_before(self):
        S.update_settings(
            github={"repos": ["o/r"], "issues_enabled": True, "issue_repos": ["o/r"]}
        )
        assert ti._pr_review_enabled() is True
        assert ti._issue_handling_enabled() is True

    def test_one_of_several_devices_does_not_unless_chosen(self, monkeypatch):
        self._fleet(monkeypatch, 2)
        S.update_settings(
            github={"repos": ["o/r"], "issues_enabled": True, "issue_repos": ["o/r"]}
        )
        assert ti._pr_review_enabled() is False
        assert ti._issue_handling_enabled() is False
        S.update_settings(github={"automation_device": "d1"})
        assert ti._pr_review_enabled() is True
        assert ti._issue_handling_enabled() is True

    def test_another_device_chosen(self, monkeypatch):
        self._fleet(monkeypatch, 2, me="d0")
        S.update_settings(github={"repos": ["o/r"], "automation_device": "d1"})
        assert ti._pr_review_enabled() is False

    def test_the_pipeline_takes_the_servers_answer(self, monkeypatch):
        """The pipeline process can't tell which device it is on (no tailnet
        identity): the server that starts it says, and restarts it when the
        answer moves."""
        from backend.ticket_ingestion.config import AUTOMATION_ENV, _merge_layers

        S.update_settings(github={"repos": ["o/r"]})
        monkeypatch.setenv(AUTOMATION_ENV, "0")
        assert _merge_layers({})["github"]["enabled"] is False  # even alone
        self._fleet(monkeypatch, 2)
        monkeypatch.setenv(AUTOMATION_ENV, "1")
        assert _merge_layers({})["github"].get("enabled", True) is not False
        ctrl = ti.TicketIngestionController()
        assert ctrl._env()[AUTOMATION_ENV] == "0"  # d1 isn't the runner
        S.update_settings(github={"automation_device": "d1"})
        assert ctrl._env()[AUTOMATION_ENV] == "1"

    def test_the_pipeline_is_told_whether_it_is_grouped(self, monkeypatch):
        """Round 4 A: the pipeline seeds a ledger that never ran when this
        device is grouped with another live one — the server says which."""
        from backend.ticket_ingestion.config import FLEET_ENV, pipeline_in_group

        ctrl = ti.TicketIngestionController()
        assert ctrl._env()[FLEET_ENV] == "0"  # alone
        self._fleet(monkeypatch, 1)
        assert ctrl._env()[FLEET_ENV] == "0"  # a group of one
        self._fleet(monkeypatch, 2)
        assert ctrl._env()[FLEET_ENV] == "1"
        monkeypatch.setenv(FLEET_ENV, "0")  # the server's answer wins
        assert pipeline_in_group() is False
        monkeypatch.delenv(FLEET_ENV)
        assert pipeline_in_group() is True  # else read here

    def test_synced_settings_never_start_a_pipeline_on_another_device(
        self, monkeypatch
    ):
        """The regression: the rig adopts github.repos + a ticket source from
        the laptop and the hook reconciles — nothing may start there."""
        self._fleet(monkeypatch, 2)
        S.update_settings(github={"repos": ["o/r"]})
        S.set_ticketing_sources([{"id": "j", "provider": "jira"}])
        addon = ti.TicketIngestionAddon()
        calls = []
        monkeypatch.setattr(addon.ctrl, "_own_running", lambda: False)
        monkeypatch.setattr(addon.ctrl, "is_running", lambda: False)
        monkeypatch.setattr(addon.ctrl, "start", lambda: calls.append("start"))
        addon._reconcile_process()
        assert calls == []

    def test_the_pipeline_itself_leaves_the_github_halves_off(self, monkeypatch):
        """A pipeline started here for tickets alone must not run the PR or
        issue loops either (they are wired from the same synced settings)."""
        from backend.ticket_ingestion.config import _merge_layers

        S.update_settings(
            github={"repos": ["o/r"], "issues_enabled": True, "issue_repos": ["o/r"]}
        )
        gh = _merge_layers({}).get("github", {})
        assert gh.get("enabled", True) is not False and gh["issues_enabled"] is True
        self._fleet(monkeypatch, 2)
        gh = _merge_layers({}).get("github", {})
        assert gh["enabled"] is False and gh["issues_enabled"] is False

    def test_joining_devices_reconciles_when_the_answer_moved(self, monkeypatch):
        import asyncio

        subs = {}

        class Ctx:
            def subscribe(self, name, cb):
                subs[name] = cb
                return lambda: None

        monkeypatch.setattr(
            ti.TicketIngestionAddon, "_process_wanted", staticmethod(lambda: False)
        )
        addon = ti.TicketIngestionAddon()
        reconciled = []
        monkeypatch.setattr(
            addon, "_reconcile_process", lambda *a: reconciled.append(1)
        )
        asyncio.run(addon.on_startup(Ctx()))
        assert {"device.joined", "device.removed"} <= set(subs)
        subs["device.joined"]({"event": "device.joined"})  # still alone
        self._fleet(monkeypatch, 2)
        subs["device.joined"]({"event": "device.joined"})
        subs["device.joined"]({"event": "device.joined"})  # no change again
        import time

        time.sleep(0.1)
        assert reconciled == [1]


class TestIngestionRepoAvailable:
    def test_true_when_provisioning_available(self, monkeypatch):
        monkeypatch.setattr(ti.provisioning, "provisioning_available", lambda: True)
        assert ti._ingestion_repo_available() is True

    def test_true_when_source_names_its_own_repo(self, monkeypatch):
        monkeypatch.setattr(ti.provisioning, "provisioning_available", lambda: False)
        S.set_ticketing_sources(
            [{"id": "j", "provider": "jira", "repo_url": "git@x:o/r.git"}]
        )
        assert ti._ingestion_repo_available() is True

    def test_false_when_nothing_configured(self, monkeypatch):
        monkeypatch.setattr(ti.provisioning, "provisioning_available", lambda: False)
        assert ti._ingestion_repo_available() is False


# --------------------------------------------------------------------------- #
# _process_wanted / _reconcile_process (toggle -> process reconciliation)
# --------------------------------------------------------------------------- #
class TestProcessWanted:
    def _patch(self, monkeypatch, *, desired, repo, pr, issues):
        monkeypatch.setattr(ti, "_desired_running", lambda: desired)
        monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: repo)
        monkeypatch.setattr(ti, "_pr_review_enabled", lambda: pr)
        monkeypatch.setattr(ti, "_issue_handling_enabled", lambda: issues)

    def test_tickets_on_needs_a_repo(self, monkeypatch):
        self._patch(monkeypatch, desired=True, repo=False, pr=False, issues=False)
        assert ti.TicketIngestionAddon._process_wanted() is False
        self._patch(monkeypatch, desired=True, repo=True, pr=False, issues=False)
        assert ti.TicketIngestionAddon._process_wanted() is True

    def test_pr_review_boots_the_pipeline_with_zero_ticket_sources(self, monkeypatch):
        """PR review (and issue handling) used to need a ticketing source to
        exist just so the config would load — a PR-only user with no tracker
        never got a pipeline. The layered config loads with none."""
        assert S.load_settings().ticketing.sources == []
        self._patch(monkeypatch, desired=False, repo=False, pr=True, issues=False)
        assert ti.TicketIngestionAddon._process_wanted() is True
        self._patch(monkeypatch, desired=False, repo=False, pr=False, issues=True)
        assert ti.TicketIngestionAddon._process_wanted() is True

    def test_nothing_on(self, monkeypatch):
        self._patch(monkeypatch, desired=False, repo=True, pr=False, issues=False)
        assert ti.TicketIngestionAddon._process_wanted() is False


class TestReconcileProcess:
    def _addon(self, monkeypatch, *, own_running, wanted, is_running=False):
        addon = ti.TicketIngestionAddon()
        calls = []
        monkeypatch.setattr(addon.ctrl, "_own_running", lambda: own_running)
        monkeypatch.setattr(addon.ctrl, "is_running", lambda: is_running)
        monkeypatch.setattr(addon.ctrl, "restart", lambda: calls.append("restart"))
        monkeypatch.setattr(addon.ctrl, "stop", lambda: calls.append("stop"))
        monkeypatch.setattr(addon.ctrl, "start", lambda: calls.append("start"))
        monkeypatch.setattr(addon, "_process_wanted", staticmethod(lambda: wanted))
        return addon, calls

    def test_own_running_and_wanted_restarts(self, monkeypatch):
        addon, calls = self._addon(monkeypatch, own_running=True, wanted=True)
        addon._reconcile_process()
        assert calls == ["restart"]

    def test_own_running_and_unwanted_stops(self, monkeypatch):
        addon, calls = self._addon(monkeypatch, own_running=True, wanted=False)
        addon._reconcile_process()
        assert calls == ["stop"]

    def test_not_running_but_wanted_starts(self, monkeypatch):
        addon, calls = self._addon(
            monkeypatch, own_running=False, wanted=True, is_running=False
        )
        addon._reconcile_process()
        assert calls == ["start"]

    def test_external_pipeline_left_alone(self, monkeypatch):
        # Not our child, but something is running (external lock) + wanted:
        # never touched.
        addon, calls = self._addon(
            monkeypatch, own_running=False, wanted=True, is_running=True
        )
        addon._reconcile_process()
        assert calls == []


# --------------------------------------------------------------------------- #
# Connectivity-error detector
# --------------------------------------------------------------------------- #
class TestRecentConnectionError:
    def _write_log(self, ctrl, lines):
        ctrl._log_path.parent.mkdir(parents=True, exist_ok=True)
        ctrl._log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_missing_log_is_false(self, ctrl):
        assert ctrl._recent_connection_error() is False

    def test_recent_error_is_true(self, ctrl):
        now = _dt.datetime.now().astimezone()
        self._write_log(
            ctrl,
            [now.isoformat() + " [ERROR] Cannot connect to host api.example.com"],
        )
        assert ctrl._recent_connection_error() is True

    def test_old_error_ages_out(self, ctrl):
        old = _dt.datetime.now().astimezone() - _dt.timedelta(hours=1)
        self._write_log(ctrl, [old.isoformat() + " [ERROR] connection refused by host"])
        assert ctrl._recent_connection_error(window_seconds=180) is False

    def test_non_connectivity_error_ignored(self, ctrl):
        now = _dt.datetime.now().astimezone()
        self._write_log(
            ctrl, [now.isoformat() + " [ERROR] SSL certificate verify failed"]
        )
        assert ctrl._recent_connection_error() is False


# --------------------------------------------------------------------------- #
# Activity beacon
# --------------------------------------------------------------------------- #
class TestActivity:
    def test_missing_beacon_is_empty(self, ctrl):
        assert ctrl._activity() == {}

    def test_parses_beacon_dict(self, ctrl):
        (ctrl._repo_root / ".mindflock-pipeline-activity.json").write_text(
            json.dumps({"pid": 42, "ticket_busy": True})
        )
        assert ctrl._activity() == {"pid": 42, "ticket_busy": True}

    def test_non_dict_beacon_is_empty(self, ctrl):
        (ctrl._repo_root / ".mindflock-pipeline-activity.json").write_text("[1,2]")
        assert ctrl._activity() == {}


# --------------------------------------------------------------------------- #
# status()
# --------------------------------------------------------------------------- #
class TestStatus:
    def test_stopped_shape(self, ctrl, monkeypatch):
        monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: False)
        st = ctrl.status()
        assert st["running"] is False
        assert st["pid"] is None
        assert st["since"] is None
        assert st["available"] is False
        # A stopped pipeline can't have a live connectivity problem.
        assert st["net_error"] is False
        assert st["log"] == str(ctrl._log_path)
        for k in ("tickets_active", "pr_active", "issues_active"):
            assert st[k] is False

    def test_activity_dropped_when_beacon_pid_mismatches(self, ctrl, monkeypatch):
        # running via an external lock at pid 500, but the beacon claims pid 7 ->
        # a dead run's beacon must not stick.
        monkeypatch.setattr(ctrl, "_own_running", lambda: False)
        monkeypatch.setattr(ctrl, "_external_lock_pid", lambda: 500)
        monkeypatch.setattr(ctrl, "_recent_connection_error", lambda *a, **k: False)
        (ctrl._repo_root / ".mindflock-pipeline-activity.json").write_text(
            json.dumps({"pid": 7, "ticket_busy": True})
        )
        st = ctrl.status()
        assert st["running"] is True and st["pid"] == 500
        assert st["tickets_active"] is False  # mismatched beacon ignored


# --------------------------------------------------------------------------- #
# Singleton-lock probe
# --------------------------------------------------------------------------- #
class TestExternalLockPid:
    def test_no_file_is_none(self, ctrl):
        assert ctrl._external_lock_pid() is None

    def test_stale_lock_reads_as_none(self, ctrl):
        # File exists but nobody holds the flock -> we can re-acquire -> stale.
        ctrl._lock_path().write_text("12345")
        assert ctrl._external_lock_pid() is None

    def test_held_lock_returns_recorded_pid(self, ctrl):
        # flock is per open-file-description: a lock held on a separate fd in
        # THIS process still blocks the non-blocking probe, so we can simulate
        # an external holder.
        p = ctrl._lock_path()
        p.write_text("777")
        fh = open(p, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            assert ctrl._external_lock_pid() == 777
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    def test_held_but_unreadable_pid_is_minus_one(self, ctrl):
        p = ctrl._lock_path()
        p.write_text("not-a-number")
        fh = open(p, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            assert ctrl._external_lock_pid() == -1
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()


class TestInheritedPipeline:
    """The server restarts itself in place (``os.execv``: Tailscale coming up,
    an update). The pipeline it started survives, still its child, but the
    new image has no ``Popen`` for it — it read as someone else's, and every
    switch flipped afterwards was silently not applied (PR review stayed off
    with its switch on). The controller now takes such a child back."""

    _HOLDER = (
        "import fcntl, json, os, sys, time\n"
        "fh = open(sys.argv[1], 'a+')\n"
        "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
        "fh.seek(0); fh.truncate()\n"
        "fh.write('%d\\n%s\\n' % (os.getpid(), json.dumps({'owner': sys.argv[2]})))\n"
        "fh.flush()\n"
        # Hold it until told to go (the file argv[3] appears), then exit
        # with code argv[4]: the flock is released as the process ends.
        "end = time.time() + 60\n"
        "while time.time() < end and not os.path.exists(sys.argv[3]):\n"
        "    time.sleep(0.02)\n"
        "sys.exit(int(sys.argv[4]))\n"
    )

    #: Generous: a loaded full run can take seconds to start an interpreter.
    _SPAWN_BUDGET = 30.0

    def _spawn(self, ctrl, owner="server", code=0):
        import subprocess
        import sys
        import time

        go = ctrl._repo_root / ("go-%s" % owner)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                self._HOLDER,
                str(ctrl._lock_path()),
                owner,
                str(go),
                str(code),
            ],
            start_new_session=True,
        )
        proc.go = go
        self._until(lambda: ctrl._external_lock_pid() == proc.pid)
        assert ctrl._external_lock_pid() == proc.pid
        return proc

    @classmethod
    def _until(cls, cond, budget=None):
        import time

        deadline = time.monotonic() + (budget or cls._SPAWN_BUDGET)
        while not cond() and time.monotonic() < deadline:
            time.sleep(0.02)
        return cond()

    def _release(self, ctrl, proc):
        """Let the holder exit and wait until its lock is free (the process
        stays a zombie until someone reaps it)."""
        proc.go.write_text("")
        assert self._until(lambda: ctrl._external_lock_pid() is None)

    @staticmethod
    def _reap(proc):
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 — already reaped by the controller
            pass

    def test_a_child_from_before_the_reexec_is_ours_again(self, ctrl):
        proc = self._spawn(ctrl)
        try:
            # A fresh controller: what the server's new image starts with.
            assert ctrl._proc is None
            assert ctrl._own_running() is True
            st = ctrl.status()
            assert st["running"] is True and st["pid"] == proc.pid
            assert st["since"]  # it says when, as for a child it started
            # …so a switch flipped now restarts it: stop() ends AND reaps it.
            assert ctrl.stop() is True
            assert ctrl._own_running() is False
            assert ctrl.is_running() is False
        finally:
            self._reap(proc)

    def test_a_standalone_run_stays_external(self, ctrl, monkeypatch):
        proc = self._spawn(ctrl, owner="cli")
        try:
            assert ctrl._own_running() is False
            assert ctrl._proc is None
            assert ctrl.is_running() is True
            # A settings change never touches it: the reconcile leaves an
            # external pipeline alone (stop() itself would end it by pid).
            addon = ti.TicketIngestionAddon()
            addon.ctrl = ctrl
            calls = []
            monkeypatch.setattr(addon, "_process_wanted", staticmethod(lambda: True))
            monkeypatch.setattr(ctrl, "start", lambda: calls.append("start"))
            monkeypatch.setattr(ctrl, "stop", lambda: calls.append("stop"))
            addon._reconcile_process(["github.enabled"])
            assert calls == []
            assert proc.poll() is None
        finally:
            self._reap(proc)

    def test_a_holder_gone_before_the_controller_looks_is_not_adopted(self, ctrl):
        proc = self._spawn(ctrl, code=3)
        try:
            self._release(ctrl, proc)  # exited (unreaped) — lock released
            assert ctrl._own_running() is False
            assert ctrl._proc is None
            st = ctrl.status()
            assert st["running"] is False and st["pid"] is None
            assert st["exit_reason"] is None and st["last_error"] is None
        finally:
            self._reap(proc)

    def test_an_adopted_child_that_exits_reports_why(self, ctrl):
        proc = self._spawn(ctrl, code=3)
        try:
            assert ctrl._own_running() is True
            adopted = ctrl._proc
            self._release(ctrl, proc)
            assert ctrl._own_running() is False
            st = ctrl.status()
            assert st["running"] is False
            assert st["exit_reason"] == "exited with code 3"
            assert st["last_error"]["kind"] == "crash"
            # The dead pid is not taken back again: same (reaped) child.
            assert ctrl._own_running() is False
            assert ctrl._proc is adopted and adopted.returncode == 3
        finally:
            self._reap(proc)

    def test_after_stopping_an_adopted_child_a_fresh_one_starts(
        self, ctrl, monkeypatch
    ):
        spawned = []

        class FakePopen:
            pid = 424242

            def __init__(self, argv, **kw):
                spawned.append(argv)

            def poll(self):
                return None

        proc = self._spawn(ctrl)
        try:
            assert ctrl._own_running() is True
            assert ctrl.stop() is True
            monkeypatch.setattr(ti.subprocess, "Popen", FakePopen)
            assert ctrl.start() is True  # not "another pipeline holds it"
            assert len(spawned) == 1 and isinstance(ctrl._proc, FakePopen)
            assert ctrl.status()["pid"] == FakePopen.pid
        finally:
            ctrl._proc = None
            self._reap(proc)

    def test_since_is_the_lock_files_mtime(self, ctrl):
        proc = self._spawn(ctrl)
        try:
            os.utime(ctrl._lock_path(), (1_700_000_000, 1_700_000_000))
            assert ctrl._own_running() is True
            assert ctrl._started_at == 1_700_000_000
            want = _dt.datetime.fromtimestamp(1_700_000_000).astimezone().isoformat()
            assert ctrl.status()["since"] == want
        finally:
            self._reap(proc)

    def test_since_is_unknown_when_the_lock_cannot_be_stat_ed(self, ctrl, monkeypatch):
        proc = self._spawn(ctrl)
        try:
            lock = ctrl._lock_path()
            monkeypatch.setattr(
                ctrl, "_lock_holder", lambda: (proc.pid, {"owner": "server"})
            )
            real_stat = pathlib.Path.stat

            def stat(self, *a, **k):
                if self == lock:
                    raise OSError("gone")
                return real_stat(self, *a, **k)

            monkeypatch.setattr(pathlib.Path, "stat", stat)
            assert ctrl._own_running() is True
            st = ctrl.status()
            assert st["running"] is True and st["pid"] == proc.pid
            assert st["since"] is None
        finally:
            monkeypatch.undo()
            self._reap(proc)

    def test_a_settings_change_restarts_an_adopted_pipeline(self, ctrl, monkeypatch):
        """The user-visible bug: after a re-exec, flipping PR review (a
        pipeline_signature change → reconcile) was skipped as "external"."""
        proc = self._spawn(ctrl)
        try:
            addon = ti.TicketIngestionAddon()
            addon.ctrl = ctrl
            started = []
            monkeypatch.setattr(addon, "_process_wanted", staticmethod(lambda: True))
            monkeypatch.setattr(ctrl, "start", lambda: started.append(1) or True)
            addon._reconcile_process(["github.enabled"])
            assert started == [1]  # restarted: stopped, then started fresh
            assert ctrl._proc is None  # the old child was stopped and reaped
            with pytest.raises(ProcessLookupError):
                os.kill(proc.pid, 0)
        finally:
            self._reap(proc)

    def test_a_server_pipeline_that_is_not_our_child_stays_external(self, ctrl):
        # Another server's: the lock says owner server, but waitpid can't
        # answer for a pid that isn't this process's child.
        p = ctrl._lock_path()
        p.write_text("%d\n%s\n" % (os.getppid(), json.dumps({"owner": "server"})))
        fh = open(p, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            assert ctrl._own_running() is False
            assert ctrl.is_running() is True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()


class TestAdoptInheritedSkips:
    """When the lock does NOT name a live child of ours, nothing is taken."""

    @staticmethod
    def _hold(ctrl, text):
        p = ctrl._lock_path()
        p.write_text(text)
        fh = open(p, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    @staticmethod
    def _unhold(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    def _assert_not_adopted(self, ctrl, *, external):
        ctrl._adopt_inherited()
        assert ctrl._proc is None
        assert ctrl._own_running() is False
        assert ctrl._proc is None
        assert ctrl.status()["running"] is external

    def test_no_lock_file(self, ctrl):
        self._assert_not_adopted(ctrl, external=False)

    def test_a_stale_lock(self, ctrl):
        ctrl._lock_path().write_text(
            "%d\n%s\n" % (os.getpid(), json.dumps({"owner": "server"}))
        )
        self._assert_not_adopted(ctrl, external=False)

    @pytest.mark.parametrize(
        "text",
        [
            "%d\n" % os.getpid(),  # an old pipeline: no line 2
            "%d\n%s\n" % (os.getpid(), json.dumps({"owner": "standalone"})),
            "0\n%s\n" % json.dumps({"owner": "server"}),
            "-5\n%s\n" % json.dumps({"owner": "server"}),
            "junk\n%s\n" % json.dumps({"owner": "server"}),
        ],
        ids=["no-meta", "standalone", "pid-0", "pid-negative", "pid-unreadable"],
    )
    def test_a_held_lock_that_is_not_the_servers_child(self, ctrl, text):
        fh = self._hold(ctrl, text)
        try:
            self._assert_not_adopted(ctrl, external=True)
        finally:
            self._unhold(fh)

    def test_a_server_lock_whose_holder_already_exited(self, ctrl):
        import subprocess
        import sys

        dead = subprocess.Popen([sys.executable, "-c", "raise SystemExit(4)"])

        # Wait for it to exit WITHOUT reaping it: a zombie child of ours.
        # macOS has no os.waitid, so there ps reports the zombie ("Z") state.
        def exited():
            if hasattr(os, "waitid"):
                return (
                    os.waitid(os.P_PID, dead.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    is not None
                )
            stat = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(dead.pid)],
                capture_output=True,
                text=True,
            ).stdout
            return "Z" in stat

        assert TestInheritedPipeline._until(exited)
        fh = self._hold(ctrl, "%d\n%s\n" % (dead.pid, json.dumps({"owner": "server"})))
        try:
            self._assert_not_adopted(ctrl, external=True)
        finally:
            self._unhold(fh)
            dead.wait(timeout=5)


class TestInheritedChild:
    """The Popen surface over a child spawned before the re-exec. Spawned
    here with ``os.posix_spawn`` so no Popen object ever waits on it."""

    @staticmethod
    def _spawn(code="import time; time.sleep(60)"):
        import sys

        return os.posix_spawn(sys.executable, [sys.executable, "-c", code], os.environ)

    @staticmethod
    def _gone(pid):
        import signal

        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (OSError, ChildProcessError):
            pass

    @pytest.mark.parametrize("code,want", [(0, 0), (7, 7)])
    def test_poll_reads_the_exit_code(self, code, want):
        child = ti._InheritedChild(self._spawn("raise SystemExit(%d)" % code))
        try:
            assert child.wait(timeout=TestInheritedPipeline._SPAWN_BUDGET) == want
            assert child.poll() == want and child.returncode == want
        finally:
            self._gone(child.pid)

    def test_poll_is_none_while_it_runs_then_minus_the_signal(self):
        import signal

        child = ti._InheritedChild(self._spawn())
        try:
            assert child.poll() is None
            os.kill(child.pid, signal.SIGKILL)
            assert child.wait(timeout=TestInheritedPipeline._SPAWN_BUDGET) == -9
        finally:
            self._gone(child.pid)

    def test_a_pid_that_is_not_our_child_is_minus_one(self):
        child = ti._InheritedChild(os.getppid())
        assert child.poll() == -1
        assert child.wait(timeout=0.1) == -1

    def test_an_exit_code_is_read_once(self, monkeypatch):
        child = ti._InheritedChild(self._spawn("raise SystemExit(2)"))
        try:
            assert child.wait(timeout=TestInheritedPipeline._SPAWN_BUDGET) == 2

            def no_more(*a):
                raise AssertionError("waitpid after the child was reaped")

            monkeypatch.setattr(ti.os, "waitpid", no_more)
            assert child.poll() == 2
            assert child.wait() == 2
        finally:
            monkeypatch.undo()
            self._gone(child.pid)

    def test_wait_times_out_on_a_running_child(self):
        import signal
        import subprocess

        child = ti._InheritedChild(self._spawn())
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=0.2)
            assert child.returncode is None
            child.terminate()
            assert (
                child.wait(timeout=TestInheritedPipeline._SPAWN_BUDGET)
                == -signal.SIGTERM
            )
        finally:
            self._gone(child.pid)

    def test_terminate_and_kill_signal_the_pid(self, monkeypatch):
        import signal

        sent = []
        monkeypatch.setattr(ti.os, "kill", lambda pid, sig: sent.append((pid, sig)))
        child = ti._InheritedChild(4321)
        child.terminate()
        child.kill()
        assert sent == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


# --------------------------------------------------------------------------- #
# Controller env / python resolution / start-guard
# --------------------------------------------------------------------------- #
class TestControllerHelpers:
    def test_python_is_the_servers_even_with_a_repo_venv(self, ctrl):
        """A repo-root .venv belongs to whatever that checkout has on disk; the
        pipeline must run the server's code (it reported 0.8.7 under 0.9.4)."""
        import sys

        venv_py = ctrl._repo_root / ".venv" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("#!/bin/sh\n")
        assert ctrl._python() == sys.executable

    def test_env_puts_the_servers_package_first(self, ctrl, monkeypatch):
        import backend

        monkeypatch.setenv("PYTHONPATH", "/existing")
        env = ctrl._env()
        parts = env["PYTHONPATH"].split(os.pathsep)
        assert parts[0] == str(pathlib.Path(backend.__file__).resolve().parent.parent)
        assert str(ctrl._repo_root / "src") in parts
        assert "/existing" in parts

    def test_the_child_never_imports_from_its_cwd(self, ctrl, monkeypatch):
        """Started with -P from a repo root holding ANOTHER backend package,
        the child still imports the server's (run for real, not mocked)."""
        import subprocess
        import sys

        import backend

        decoy = ctrl._repo_root / "backend"
        decoy.mkdir(parents=True, exist_ok=True)
        (decoy / "__init__.py").write_text("DECOY = True\n")
        out = subprocess.run(
            [ctrl._python(), "-P", "-c", "import backend; print(backend.__file__)"],
            cwd=str(ctrl._repo_root),
            env=ctrl._env(),
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
        assert out == str(pathlib.Path(backend.__file__).resolve())

    def test_start_noops_when_already_running(self, ctrl, monkeypatch):
        monkeypatch.setattr(ctrl, "is_running", lambda: True)
        assert ctrl.start() is False  # never spawns a second pipeline


# --------------------------------------------------------------------------- #
# Endpoints (router-only app — no full server, no subprocess spawn)
# --------------------------------------------------------------------------- #
@pytest.fixture
def endpoint_client(monkeypatch):
    addon = ti.TicketIngestionAddon()
    app = FastAPI()
    app.include_router(addon.router)
    return TestClient(app), addon


class TestEndpoints:
    def test_status_endpoint_returns_dict(self, endpoint_client, monkeypatch):
        client, addon = endpoint_client
        monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: False)
        # status() falls back to the on-disk pipeline lock, so without this the
        # assertion below reads whether the developer's own ingestion run is up
        # — it failed on any machine that had the pipeline going.
        monkeypatch.setattr(addon.ctrl, "_external_lock_pid", lambda: None)
        r = client.get("/api/mindflock/status")
        assert r.status_code == 200
        assert r.json()["running"] is False

    def test_status_says_where_pr_review_runs(self, endpoint_client, monkeypatch):
        """On a device that doesn't run them, Intake must say "runs on
        <device>" — not "switch Automated PR review on" while it is on (and
        synced: flipping it would pause it on the device that does run it)."""
        from backend.web.core import remote, settings_hooks

        client, addon = endpoint_client
        monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: False)
        monkeypatch.setattr(addon.ctrl, "_external_lock_pid", lambda: None)
        S.update_settings(github={"repos": ["o/r"], "automation_device": "laptop"})
        from backend.web.core import fleet

        monkeypatch.setattr(fleet, "in_fleet", lambda: True)
        monkeypatch.setattr(
            fleet,
            "live_members",
            lambda: {"laptop": {"host": "Laptop"}, "mac": {"host": "Mac"}},
        )
        monkeypatch.setattr(remote, "self_identity", lambda: {"key": "mac"})
        body = client.get("/api/mindflock/status").json()
        assert body["automation_here"] is False
        assert body["automation_device"] == "Laptop"
        S.update_settings(github={"automation_device": "mac"})
        body = client.get("/api/mindflock/status").json()
        assert body["automation_here"] is True and body["automation_device"] == ""
        assert settings_hooks.automation_here() is True

    def test_start_blocked_without_repo(self, endpoint_client, monkeypatch):
        client, _ = endpoint_client
        monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: False)
        r = client.post("/api/mindflock/start")
        assert r.status_code == 400
        assert "No repo to ingest into" in r.json()["error"]


# --------------------------------------------------------------------------- #
# After an engine update: a pipeline still on the old build is restarted
# --------------------------------------------------------------------------- #
class TestStaleBuild:
    def _hold(self, ctrl, text):
        p = ctrl._lock_path()
        p.write_text(text)
        fh = open(p, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def _release(self, fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    def _build(self, monkeypatch, version="9.9.9", commit=""):
        from backend.web.core import self_update

        monkeypatch.setattr(self_update, "installed_version", lambda: version)
        monkeypatch.setattr(self_update, "installed_commit", lambda: commit)

    def test_the_lock_records_the_build_and_the_pid_still_parses(
        self, ctrl, monkeypatch
    ):
        import json as _json

        self._build(monkeypatch)
        fh = self._hold(
            ctrl,
            "321\n"
            + _json.dumps({"version": "0.7.4", "commit": "", "owner": "server"}),
        )
        try:
            assert ctrl._external_lock_pid() == 321
            assert ctrl._lock_holder()[1]["version"] == "0.7.4"
            assert ctrl.stale_build() is True
        finally:
            self._release(fh)

    def test_the_same_build_is_not_stale(self, ctrl, monkeypatch):
        import json as _json

        self._build(monkeypatch, commit="a" * 40)
        fh = self._hold(
            ctrl,
            "321\n"
            + _json.dumps({"version": "0.7.4", "commit": "a" * 40, "owner": "server"}),
        )
        try:
            # Same commit wins over a version string that disagrees.
            assert ctrl.stale_build() is False
        finally:
            self._release(fh)

    def test_a_lock_from_before_builds_were_recorded_is_stale(self, ctrl, monkeypatch):
        self._build(monkeypatch)
        fh = self._hold(ctrl, "321")
        try:
            assert ctrl.stale_build() is True
        finally:
            self._release(fh)

    def test_a_standalone_run_is_never_touched(self, ctrl, monkeypatch):
        import json as _json

        self._build(monkeypatch)
        fh = self._hold(ctrl, "321\n" + _json.dumps({"version": "0.1.0", "owner": ""}))
        try:
            assert ctrl.stale_build() is False
        finally:
            self._release(fh)

    def test_nothing_running_is_not_stale(self, ctrl):
        assert ctrl.stale_build() is False

    def test_boot_restarts_a_stale_pipeline_instead_of_leaving_it(self, monkeypatch):
        addon = ti.TicketIngestionAddon()
        calls = []
        monkeypatch.setattr(ti, "_may_restart_found_pipeline", lambda: True)
        monkeypatch.setattr(
            ti.TicketIngestionAddon, "_process_wanted", staticmethod(lambda: True)
        )
        monkeypatch.setattr(addon.ctrl, "stale_build", lambda: True)
        monkeypatch.setattr(addon.ctrl, "restart", lambda: calls.append("restart"))
        monkeypatch.setattr(addon.ctrl, "start", lambda: calls.append("start"))
        addon._boot_reconcile()
        assert calls == ["restart"]

    def test_boot_leaves_a_stale_pipeline_alone_with_every_toggle_off(
        self, monkeypatch
    ):
        """A restart is a stop AND a start: with the toggles off it would turn
        on what the user left off — and an owner-unknown lock (no metadata,
        maybe a standalone run) is only touched while the pipeline is wanted."""
        addon = ti.TicketIngestionAddon()
        calls = []
        monkeypatch.setattr(ti, "_may_restart_found_pipeline", lambda: True)
        monkeypatch.setattr(
            ti.TicketIngestionAddon, "_process_wanted", staticmethod(lambda: False)
        )
        monkeypatch.setattr(addon.ctrl, "stale_build", lambda: True)
        monkeypatch.setattr(addon.ctrl, "restart", lambda: calls.append("restart"))
        monkeypatch.setattr(addon.ctrl, "start", lambda: calls.append("start"))
        monkeypatch.setattr(addon.ctrl, "stop", lambda: calls.append("stop"))
        addon._boot_reconcile()
        assert calls == []

    def test_boot_never_restarts_a_found_pipeline_under_pytest(self, monkeypatch):
        addon = ti.TicketIngestionAddon()
        monkeypatch.setattr(
            addon.ctrl, "stale_build", lambda: pytest.fail("probed the real lock")
        )
        monkeypatch.setattr(
            ti.TicketIngestionAddon, "_process_wanted", staticmethod(lambda: False)
        )
        addon._boot_reconcile()

    def test_the_child_is_marked_as_the_servers(self, ctrl):
        assert ctrl._env()[ti.PIPELINE_OWNER_ENV] == "server"

    def test_the_pipeline_writes_pid_then_build(self, tmp_path, monkeypatch):
        import json as _json

        from backend.ticket_ingestion import __main__ as pipeline_main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(pipeline_main.OWNER_ENV, "server")
        monkeypatch.setattr(pipeline_main, "_LOCK_HANDLE", None)
        assert pipeline_main._acquire_singleton_lock() is True
        try:
            lines = (tmp_path / ".mindflock-pipeline.lock").read_text().splitlines()
            assert int(lines[0]) == os.getpid()
            meta = _json.loads(lines[1])
            assert meta["owner"] == "server" and "version" in meta and "commit" in meta
            assert pipeline_main.OWNER_ENV == ti.PIPELINE_OWNER_ENV
        finally:
            pipeline_main._LOCK_HANDLE.close()
