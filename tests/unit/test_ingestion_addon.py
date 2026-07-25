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


class TestTicketingConfigured:
    def test_false_when_no_source(self):
        assert ti._ticketing_configured() is False

    def test_true_when_a_source_has_provider(self):
        S.set_ticketing_sources([{"id": "j", "provider": "jira"}])
        assert ti._ticketing_configured() is True


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
    def _patch(self, monkeypatch, *, desired, repo, pr, issues, ticketing):
        monkeypatch.setattr(ti, "_desired_running", lambda: desired)
        monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: repo)
        monkeypatch.setattr(ti, "_pr_review_enabled", lambda: pr)
        monkeypatch.setattr(ti, "_issue_handling_enabled", lambda: issues)
        monkeypatch.setattr(ti, "_ticketing_configured", lambda: ticketing)

    def test_tickets_on_needs_a_repo(self, monkeypatch):
        self._patch(
            monkeypatch,
            desired=True,
            repo=False,
            pr=False,
            issues=False,
            ticketing=True,
        )
        assert ti.TicketIngestionAddon._process_wanted() is False
        self._patch(
            monkeypatch, desired=True, repo=True, pr=False, issues=False, ticketing=True
        )
        assert ti.TicketIngestionAddon._process_wanted() is True

    def test_pr_only_needs_a_ticketing_source(self, monkeypatch):
        self._patch(
            monkeypatch,
            desired=False,
            repo=False,
            pr=True,
            issues=False,
            ticketing=False,
        )
        assert ti.TicketIngestionAddon._process_wanted() is False
        self._patch(
            monkeypatch,
            desired=False,
            repo=False,
            pr=True,
            issues=False,
            ticketing=True,
        )
        assert ti.TicketIngestionAddon._process_wanted() is True

    def test_nothing_on(self, monkeypatch):
        self._patch(
            monkeypatch,
            desired=False,
            repo=True,
            pr=False,
            issues=False,
            ticketing=True,
        )
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


# --------------------------------------------------------------------------- #
# Controller env / python resolution / start-guard
# --------------------------------------------------------------------------- #
class TestControllerHelpers:
    def test_python_prefers_repo_venv(self, ctrl):
        venv_py = ctrl._repo_root / ".venv" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("#!/bin/sh\n")
        assert ctrl._python() == str(venv_py)

    def test_python_falls_back_to_sys_executable(self, ctrl):
        import sys

        assert ctrl._python() == sys.executable  # no repo .venv present

    def test_env_prepends_src_to_pythonpath(self, ctrl, monkeypatch):
        monkeypatch.setenv("PYTHONPATH", "/existing")
        env = ctrl._env()
        parts = env["PYTHONPATH"].split(os.pathsep)
        assert parts[0] == str(ctrl._repo_root / "src")
        assert "/existing" in parts

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
        client, _ = endpoint_client
        monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: False)
        r = client.get("/api/mindflock/status")
        assert r.status_code == 200
        assert r.json()["running"] is False

    def test_start_blocked_without_repo(self, endpoint_client, monkeypatch):
        client, _ = endpoint_client
        monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: False)
        r = client.post("/api/mindflock/start")
        assert r.status_code == 400
        assert "No repo to ingest into" in r.json()["error"]
