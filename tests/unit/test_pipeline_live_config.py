"""The ticket pipeline follows settings changes and reports why it is unwell.

* the restart signature (``settings_hooks.pipeline_signature``) covers every
  field of every ticket source (secrets as a hash), the GitHub token / repos /
  switches and which device runs them — and names what moved;
* a LOCAL save (``POST /api/settings``, ``PUT /api/settings/ticketing/sources``)
  reconciles the pipeline the way a synced one does: once, debounced;
* ``/api/mindflock/status`` classifies what is wrong — a tracker or GitHub
  refusing the token (``auth``, named), a source the loader skipped
  (``config``, source + field), a connectivity failure (``network``), the
  child exiting (``exit_reason`` + a ``crash`` / ``config`` error);
* a source with a missing required field is skipped by the layered loader,
  the others keep running; PR review boots with zero ticket sources;
* ``MINDFLOCK_TICKET_PROVIDER`` is announced in the status payload.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.ticket_ingestion import __main__ as pipeline_main
from backend.ticket_ingestion.config import ConfigError, load_config
from backend.web import server
from backend.web.addons import ticket_ingestion as ti
from backend.web.core import events, settings_hooks


@pytest.fixture(autouse=True)
def _isolate(isolate_settings_store, monkeypatch):
    monkeypatch.setattr(settings_hooks, "_last_sig", None)
    monkeypatch.delenv(ti.TICKET_PROVIDER_ENV, raising=False)
    yield
    with settings_hooks._TIMER_LOCK:
        if settings_hooks._pipeline_timer is not None:
            settings_hooks._pipeline_timer.cancel()
            settings_hooks._pipeline_timer = None
        settings_hooks._pending_changes.clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    return TestClient(server.app)


@pytest.fixture
def pipeline_events():
    """Every pipeline-reconcile event emitted during the test."""
    seen = []

    def on(env):
        if env["event"] == settings_hooks.PIPELINE_EVENT:
            seen.append(env)

    unsubscribe = events.BUS.subscribe(on)
    yield seen
    unsubscribe()


class _FakeTimer:
    """Stands in for ``threading.Timer`` in :mod:`settings_hooks`: records
    each timer instead of starting a thread, so a test fires the one the
    debounce left armed itself — a slow CI runner can't stretch the gap
    between two saves past a real debounce, or the timer past a wait."""

    def __init__(self, made, interval, fn):
        self.interval, self.fn, self.cancelled = interval, fn, False
        made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def fake_timers(monkeypatch):
    """Every pipeline timer the debounce arms during the test (see
    :class:`_FakeTimer`); ``armed(made)`` lists the ones not cancelled."""
    made = []
    monkeypatch.setattr(
        settings_hooks,
        "threading",
        type(
            "_T",
            (),
            {"Timer": staticmethod(lambda i, fn: _FakeTimer(made, i, fn))},
        ),
    )
    return made


def _armed(made):
    return [t for t in made if not t.cancelled]


def _wait_for(seen, n=1, timeout=2.0):
    deadline = _dt.datetime.now() + _dt.timedelta(seconds=timeout)
    while len(seen) < n and _dt.datetime.now() < deadline:
        threading.Event().wait(0.02)
    return seen


SC = {"id": "sc", "provider": "shortcut", "api_token": "tok-1", "member_id": "m1"}


# --------------------------------------------------------------------------- #
# 1. the signature covers every field class
# --------------------------------------------------------------------------- #
def _sig_after(**source_fields):
    S.set_ticketing_sources([{**SC, **source_fields}])
    return settings_hooks.pipeline_signature()


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_token", "tok-2"),  # a rotated token
        ("member_id", "m2"),
        ("assignee_scope", "anyone"),
        ("ingest_labels", "mindflock"),
        ("base_url", "https://acme.example"),
        ("email", "me@acme.example"),
        ("repo_url", "git@x:o/r.git"),
        ("workflow_state", "500"),
        ("project", "PROJ"),
        ("agent", "codex"),
        ("label", "Renamed"),
    ],
)
def test_every_ticket_source_field_moves_the_signature(field, value):
    """Today only id/provider/project/state/repo/agent counted: a pasted
    token, a changed scope or label filter never reached the pipeline."""
    before = _sig_after()
    after = _sig_after(**{field: value})
    assert after != before
    assert settings_hooks.signature_changes(before, after) == ["ticketing.sc"]


def test_secrets_enter_the_signature_only_as_a_hash():
    sig = _sig_after(api_token="super-secret-token")
    assert "super-secret-token" not in repr(sig)
    S.update_settings(github={"token": "ghp_plain"})
    assert "ghp_plain" not in repr(settings_hooks.pipeline_signature())


def test_same_values_same_signature():
    assert _sig_after(member_id="m9") == _sig_after(member_id="m9")


@pytest.mark.parametrize(
    "patch",
    [
        {"token": "ghp_new"},
        {"repos": ["o/r"]},
        {"issue_repos": ["o/r"]},
        {"enabled": False},
        {"issues_enabled": True},
    ],
)
def test_github_token_repos_and_switches_move_the_signature(patch):
    before = settings_hooks.pipeline_signature()
    S.update_settings(github=patch)
    after = settings_hooks.pipeline_signature()
    assert settings_hooks.signature_changes(before, after) == ["github"]


def test_per_poll_github_knobs_leave_the_signature_alone():
    before = settings_hooks.pipeline_signature()
    S.update_settings(github={"min_age_minutes": 30, "skip_authors": ["bot"]})
    assert (
        settings_hooks.signature_changes(before, settings_hooks.pipeline_signature())
        == []
    )


def test_adding_and_removing_a_source_is_named():
    before = settings_hooks.pipeline_signature()
    S.set_ticketing_sources([SC, {"id": "j", "provider": "jira"}])
    mid = settings_hooks.pipeline_signature()
    assert settings_hooks.signature_changes(before, mid) == [
        "ticketing.j",
        "ticketing.sc",
    ]
    S.set_ticketing_sources([SC])
    assert settings_hooks.signature_changes(
        mid, settings_hooks.pipeline_signature()
    ) == ["ticketing.j"]


def test_signature_changes_treats_unknown_as_everything():
    sig = _sig_after()
    assert settings_hooks.signature_changes(None, sig) == [
        "automation",
        "github",
        "ticketing.sc",
    ]
    assert settings_hooks.signature_changes(sig, sig) == []


# --------------------------------------------------------------------------- #
# 2. a local write restarts the pipeline once (debounced)
# --------------------------------------------------------------------------- #
def test_a_local_github_token_save_restarts_the_pipeline_once(
    client, pipeline_events, fake_timers
):
    """A pasted token used to take effect only after a manual off/on: the
    settings route never ran ``after_settings_change`` for its own writes."""
    r = client.post("/api/settings", json={"github": {"token": "ghp_one"}})
    assert r.status_code == 200
    # A burst of blur-saves: the repos field, then the token again.
    client.post("/api/settings", json={"github": {"repos": ["o/r"]}})
    client.post("/api/settings", json={"github": {"token": "ghp_two"}})
    assert pipeline_events == []  # not immediate
    armed = _armed(fake_timers)
    assert len(armed) == 1  # each save pushed the one timer back
    assert armed[0].interval == settings_hooks.PIPELINE_DEBOUNCE
    armed[0].fn()
    assert len(pipeline_events) == 1
    assert pipeline_events[0]["data"] == {"changed": ["github"]}


def test_one_post_spanning_github_and_repository_writes_both_restarts_once(
    client, pipeline_events, fake_timers
):
    """Intake's "Use it for" ticks put one repository on the PR list, the
    issue list and Verify's list in ONE ``POST /api/settings`` (the frontend's
    ``saveGroups``): both groups land, and the pipeline restarts once."""
    r = client.post(
        "/api/settings",
        json={
            "github": {
                "repos": ["acme/web"],
                "enabled": False,
                "issue_repos": ["acme/web"],
            },
            "repository": {"verify_repos": ["acme/web"]},
        },
    )
    assert r.status_code == 200, r.text
    st = S.load_settings()
    assert st.github.repos == ["acme/web"]
    assert st.github.issue_repos == ["acme/web"]
    assert st.github.enabled is False
    assert st.repository.verify_repos == ["acme/web"]
    armed = _armed(fake_timers)
    assert len(armed) == 1
    armed[0].fn()
    assert len(pipeline_events) == 1
    assert pipeline_events[0]["data"] == {"changed": ["github"]}


def test_a_local_ticket_source_save_restarts_the_pipeline_once(
    client, pipeline_events, fake_timers
):
    r = client.put("/api/settings/ticketing/sources", json={"sources": [SC]})
    assert r.status_code == 200, r.text
    client.put(
        "/api/settings/ticketing/sources",
        json={"sources": [{**SC, "assignee_scope": "anyone", "workflow_state": "5"}]},
    )
    armed = _armed(fake_timers)
    assert len(armed) == 1
    armed[0].fn()
    assert len(pipeline_events) == 1
    assert pipeline_events[0]["data"] == {"changed": ["ticketing.sc"]}


def test_a_local_save_that_moves_nothing_restarts_nothing(
    client, pipeline_events, monkeypatch
):
    monkeypatch.setattr(settings_hooks, "PIPELINE_DEBOUNCE", 0.02)
    S.update_settings(github={"repos": ["o/r"]})
    client.post("/api/settings", json={"github": {"min_age_minutes": 45}})
    client.post("/api/settings", json={"ui": {"accent": "#f00"}})
    S.set_ticketing_sources([SC])
    client.put("/api/settings/ticketing/sources", json={"sources": [SC]})
    threading.Event().wait(0.2)
    assert pipeline_events == []


def test_the_restart_logs_what_changed_never_values(monkeypatch):
    from backend import log as _log

    lines = []

    class Log:
        def Printf(self, fmt, *args):
            lines.append(fmt % args)

    monkeypatch.setattr(_log, "ErrorLog", Log())
    addon = ti.TicketIngestionAddon()
    monkeypatch.setattr(addon.ctrl, "_own_running", lambda: True)
    monkeypatch.setattr(addon.ctrl, "restart", lambda: True)
    monkeypatch.setattr(addon, "_process_wanted", staticmethod(lambda: True))
    addon._reconcile_process(["github", "ticketing.sc"])
    assert lines == [
        "ingestion: settings changed (github, ticketing.sc) — restarting the pipeline"
    ]


def test_the_toggle_event_hands_the_changed_keys_to_the_reconcile(monkeypatch):
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
    got = []
    monkeypatch.setattr(
        addon, "_reconcile_process", lambda changed: got.append(changed)
    )
    asyncio.run(addon.on_startup(Ctx()))
    subs[settings_hooks.PIPELINE_EVENT](
        {"event": settings_hooks.PIPELINE_EVENT, "data": {"changed": ["github"]}}
    )
    subs[settings_hooks.PIPELINE_EVENT]({"event": settings_hooks.PIPELINE_EVENT})
    threading.Event().wait(0.1)
    assert got == [["github"], []]


# --------------------------------------------------------------------------- #
# 3. status: auth / network / config / crash
# --------------------------------------------------------------------------- #
@pytest.fixture
def ctrl(tmp_path):
    c = ti.TicketIngestionController()
    c._repo_root = tmp_path
    c._log_path = tmp_path / "logs" / "ticket-ingestion.log"
    c._log_path.parent.mkdir(parents=True)
    return c


def _log(ctrl, *lines, age=0):
    ts = (_dt.datetime.now() - _dt.timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%S")
    with open(ctrl._log_path, "a") as fh:
        for line in lines:
            fh.write("%s %s\n" % (ts, line))


def _running(ctrl, monkeypatch, pid=500):
    monkeypatch.setattr(ctrl, "_own_running", lambda: False)
    monkeypatch.setattr(ctrl, "_external_lock_pid", lambda: pid)
    monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: True)


@pytest.mark.parametrize(
    "line,source",
    [
        (
            "[ERROR] backend.ticket_ingestion.orchestrator: Poll scan for 'sc' "
            "failed: Shortcut rejected the token (HTTP 401); will retry next interval",
            "sc",
        ),
        (
            "[ERROR] backend.ticket_ingestion.pr_monitor: GitHub /pulls returned "
            "401 for o/r: Bad credentials",
            "GitHub o/r",
        ),
        (
            "[ERROR] backend.ticket_ingestion.providers.jira: Jira rejected the "
            "credentials (HTTP 403)",
            "jira",
        ),
        (
            "[ERROR] backend.ticket_ingestion.providers.linear: HTTP 403 Forbidden",
            "linear",
        ),
    ],
)
def test_a_rejected_token_is_an_auth_error_naming_the_source(
    ctrl, monkeypatch, line, source
):
    """Auth failures were deliberately dropped by the connectivity regex, so a
    revoked token showed no indicator at all."""
    _running(ctrl, monkeypatch)
    _log(ctrl, line)
    st = ctrl.status()
    assert st["last_error"]["kind"] == "auth"
    assert st["last_error"]["source"] == source
    assert st["last_error"]["message"].startswith(line.split(": ", 1)[1][:30])
    assert st["net_error"] is False  # not an outage
    assert st["exit_reason"] is None


def test_a_pr_or_ticket_numbered_403_is_not_an_auth_error(ctrl, monkeypatch):
    _running(ctrl, monkeypatch)
    _log(ctrl, "[INFO] backend.ticket_ingestion.pr_monitor: Reviewing PR #403 in o/r")
    _log(ctrl, "[INFO] backend.ticket_ingestion.orchestrator: Processing sc-401")
    assert ctrl.status()["last_error"] is None


def test_a_connectivity_failure_is_a_network_error_and_net_error(ctrl, monkeypatch):
    _running(ctrl, monkeypatch)
    _log(
        ctrl,
        "[WARNING] backend.ticket_ingestion.backfill: Backfill API error "
        "(attempt 1/3): Cannot connect to host api.app.shortcut.com:443",
    )
    st = ctrl.status()
    assert st["net_error"] is True
    assert st["last_error"]["kind"] == "network"


def test_the_newest_error_wins_and_old_ones_age_out(ctrl, monkeypatch):
    _running(ctrl, monkeypatch)
    _log(ctrl, "[ERROR] x: Shortcut rejected the token (HTTP 401)", age=5)
    _log(ctrl, "[ERROR] x: Cannot connect to host api.app.shortcut.com:443", age=2)
    assert ctrl.status()["last_error"]["kind"] == "network"
    ctrl._log_path.write_text("")
    _log(
        ctrl,
        "[ERROR] x: Shortcut rejected the token (HTTP 401)",
        age=ti.ERROR_WINDOW + 60,
    )
    st = ctrl.status()
    assert st["last_error"] is None and st["net_error"] is False


def test_errors_are_not_reported_for_a_stopped_pipeline(ctrl, monkeypatch):
    monkeypatch.setattr(ctrl, "_external_lock_pid", lambda: None)
    monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: False)
    _log(ctrl, "[ERROR] x: Shortcut rejected the token (HTTP 401)")
    st = ctrl.status()
    assert st["running"] is False
    assert st["last_error"] is None and st["exit_reason"] is None


def test_a_skipped_source_is_a_config_error_naming_source_and_field(ctrl, monkeypatch):
    _running(ctrl, monkeypatch, pid=500)
    (ctrl._repo_root / ".mindflock-pipeline-activity.json").write_text(
        json.dumps(
            {
                "pid": 500,
                "ticket_busy": 0,
                "updated": "2026-10-10T10:00:00+00:00",
                "config_errors": [
                    {
                        "source": "sc",
                        "field": "member_id",
                        "message": "ticketing.member_id (your Shortcut member id)",
                    }
                ],
            }
        )
    )
    _log(ctrl, "[ERROR] x: Jira rejected the credentials (HTTP 401)")
    st = ctrl.status()
    assert st["config_errors"] == [
        {
            "source": "sc",
            "field": "member_id",
            "message": "ticketing.member_id (your Shortcut member id)",
        }
    ]
    assert st["last_error"] == {
        "kind": "config",
        "message": "sc: missing member_id",
        "at": "2026-10-10T10:00:00+00:00",
        "source": "sc",
        "field": "member_id",
    }


class _Proc:
    def __init__(self, pid, returncode):
        self.pid, self.returncode = pid, returncode

    def poll(self):
        return self.returncode


def test_a_config_error_exit_names_the_missing_field(ctrl, monkeypatch):
    """ "starting, or the pipeline exited" becomes the exact field."""
    monkeypatch.chdir(ctrl._repo_root)
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    pipeline_main._write_exit_record(
        "config",
        "Invalid configuration in config.toml. Missing or invalid fields:\n"
        "  - ticketing.member_id (your Shortcut member id)\n"
        "  - repository.url (or a per-source repo_url on each ticketing source)",
    )
    rec = json.loads((ctrl._repo_root / ti.EXIT_RECORD).read_text())
    assert rec["pid"] == 4242 and rec["kind"] == "config"
    assert rec["fields"] == ["ticketing.member_id", "repository.url"]
    ctrl._proc = _Proc(4242, 1)
    monkeypatch.setattr(ctrl, "_external_lock_pid", lambda: None)
    monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: True)
    st = ctrl.status()
    assert st["running"] is False
    assert st["exit_reason"] == "configuration error"
    assert st["last_error"]["kind"] == "config"
    assert st["last_error"]["fields"] == ["ticketing.member_id", "repository.url"]
    assert st["last_error"]["message"] == (
        "missing or invalid: ticketing.member_id, repository.url"
    )
    assert st["last_error"]["at"] == rec["at"]


def test_an_exit_record_from_another_pipeline_is_not_ours(ctrl, monkeypatch):
    monkeypatch.chdir(ctrl._repo_root)
    monkeypatch.setattr(os, "getpid", lambda: 1)
    pipeline_main._write_exit_record("config", "x")
    ctrl._proc = _Proc(4242, 1)
    monkeypatch.setattr(ctrl, "_external_lock_pid", lambda: None)
    monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: True)
    st = ctrl.status()
    assert st["exit_reason"] == "exited with code 1"
    assert st["last_error"]["kind"] == "crash"


def test_a_crash_exit_reads_the_last_error_line(ctrl, monkeypatch):
    ctrl._proc = _Proc(77, -9)
    monkeypatch.setattr(ctrl, "_external_lock_pid", lambda: None)
    monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: True)
    st = ctrl.status()
    assert st["exit_reason"] == "killed by signal 9"
    assert st["last_error"] == {
        "kind": "crash",
        "message": "killed by signal 9",
        "at": st["last_error"]["at"],
        "source": "",
    }
    ctrl._proc = _Proc(78, 0)
    st = ctrl.status()
    assert st["exit_reason"] == "exited (another pipeline holds the lock)"
    assert st["last_error"] is None


def test_a_fresh_start_clears_the_previous_exit(ctrl, monkeypatch):
    (ctrl._repo_root / ti.EXIT_RECORD).write_text("{}")
    ctrl._exited_at = 1.0
    monkeypatch.setattr(ctrl, "is_running", lambda: False)

    class Popen:
        def __init__(self, *a, **k):
            self.pid = 1

    monkeypatch.setattr(ti.subprocess, "Popen", Popen)
    assert ctrl.start() is True
    assert ctrl._exited_at is None
    assert not (ctrl._repo_root / ti.EXIT_RECORD).exists()


# --------------------------------------------------------------------------- #
# 4. a broken source never crashes the child; PR review boots alone
# --------------------------------------------------------------------------- #
def test_a_source_missing_a_required_field_is_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    S.update_settings(repository={"url": "git@github.com:o/r.git"})
    S.set_ticketing_sources(
        [
            {"id": "sc", "provider": "shortcut", "api_token": "tok"},  # no member_id
            {
                "id": "j",
                "provider": "jira",
                "api_token": "tok",
                "email": "me@x",
                "base_url": "https://x.atlassian.net",
            },
        ]
    )
    cfg = load_config()
    assert [s.id for s in cfg.ticketing_sources] == ["j"]
    assert cfg.tickets_enabled is True
    assert [e.to_dict() for e in cfg.source_errors] == [
        {
            "source": "sc",
            "field": "member_id",
            "message": "ticketing.source[0].member_id (your Shortcut member id)",
        }
    ]


def test_a_source_missing_only_a_test_filled_field_is_skipped_not_fatal(
    tmp_path, monkeypatch
):
    # Layered mode: a source saved before Test filled its ``auto`` field (the
    # Shortcut member id, the Asana workspace) skips just that source.
    monkeypatch.chdir(tmp_path)
    S.update_settings(repository={"url": "git@github.com:o/r.git"})
    S.set_ticketing_sources(
        [
            {"id": "sc", "provider": "shortcut", "api_token": "tok"},
            {"id": "as", "provider": "asana", "api_token": "tok"},
            # Asana's user gid is auto but optional: blank is fine.
            {"id": "as2", "provider": "asana", "api_token": "tok", "project": "12"},
            {"id": "ln", "provider": "linear", "api_token": "tok"},
        ]
    )
    cfg = load_config()  # no raise
    assert [s.id for s in cfg.ticketing_sources] == ["as2", "ln"]
    assert cfg.tickets_enabled is True
    assert [(e.source, e.field) for e in cfg.source_errors] == [
        ("sc", "member_id"),
        ("as", "project"),
    ]


def test_a_source_with_no_repo_is_skipped_naming_repo_url(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    S.set_ticketing_sources(
        [
            {"id": "a", "provider": "linear", "api_token": "t"},
            {
                "id": "b",
                "provider": "linear",
                "api_token": "t",
                "repo_url": "git@x:o/r",
            },
        ]
    )
    cfg = load_config()
    assert [s.id for s in cfg.ticketing_sources] == ["b"]
    assert [(e.source, e.field) for e in cfg.source_errors] == [("a", "repo_url")]


def test_every_source_broken_means_tickets_off_but_pr_review_up(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    S.update_settings(
        general={"ingestion_autostart": True},
        github={"repos": ["o/r"], "enabled": True},
    )
    S.set_ticketing_sources([{"id": "sc", "provider": "shortcut", "api_token": "t"}])
    cfg = load_config()  # no raise
    assert cfg.tickets_enabled is False
    assert cfg.github is not None and cfg.github.repo_list() == ["o/r"]
    assert [(e.source, e.field) for e in cfg.source_errors] == [("sc", "member_id")]


def test_pr_review_boots_with_zero_ticket_sources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    S.update_settings(github={"repos": ["o/r"], "enabled": True})
    cfg = load_config()
    assert cfg.tickets_enabled is False and cfg.source_errors == []
    assert cfg.github.enabled is True


def test_an_explicit_config_file_still_refuses_a_missing_field(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[ticketing]\nprovider = "shortcut"\napi_token = "t"\n'
        '[repository]\nurl = "git@x:o/r"\n'
    )
    with pytest.raises(ConfigError, match="member_id"):
        load_config(path)


def test_the_orchestrator_mirrors_skipped_sources_to_the_beacon(tmp_path, monkeypatch):
    from backend.ticket_ingestion import orchestrator as orch
    from backend.ticket_ingestion.config import (
        PipelineConfig,
        SourceConfigError,
        TicketProviderConfig,
    )

    monkeypatch.setattr(orch, "_STATE_DIR", tmp_path)
    cfg = PipelineConfig(
        ticketing=TicketProviderConfig(provider="linear", api_token="t", id="b"),
        source_errors=[SourceConfigError("a", "api_token", "ticketing.api_token (x)")],
    )
    o = orch.PipelineOrchestrator(cfg)
    o._write_activity()
    beacon = json.loads((tmp_path / orch._ACTIVITY_FILE).read_text())
    assert beacon["config_errors"] == [
        {"source": "a", "field": "api_token", "message": "ticketing.api_token (x)"}
    ]


# --------------------------------------------------------------------------- #
# 5. the env override is announced
# --------------------------------------------------------------------------- #
def test_the_env_provider_override_is_in_the_status(ctrl, monkeypatch):
    monkeypatch.setattr(ctrl, "_external_lock_pid", lambda: None)
    monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: False)
    assert ctrl.status()["env_override"] is None
    monkeypatch.setenv(ti.TICKET_PROVIDER_ENV, "jira")
    assert ctrl.status()["env_override"] == {
        "provider": "jira",
        "hides_ui_sources": True,
    }
