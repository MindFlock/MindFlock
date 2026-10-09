"""Under pytest, no store may resolve into the owner's real ``~/.mindflock``.

The conftest redirects are fixtures, and a scratch suite that does
``from tests.conftest import *`` skips every underscore-named one — which is how
a review once wrote fake team runs and armed autopilot records into a live
store. These tests strip the redirects and prove each resolver refuses on its
own. They only ever call RESOLVERS (never a writer), so a broken guard fails
the test instead of touching the real files.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from backend.config import home_guard
from backend.config.home_guard import RealHomeStoreError

_REDIRECTS = (
    "MINDFLOCK_SETTINGS_FILE",
    "MINDFLOCK_PORTS_FILE",
    "MINDFLOCK_TEST_PLANS_FILE",
    "MINDFLOCK_AUTOPILOT_FILE",
    "MINDFLOCK_PROMPT_QUEUE_FILE",
    "MINDFLOCK_RUNS_DIR",
    "MINDFLOCK_MAILBOX_FILE",
    "MINDFLOCK_RED_ZONES_FILE",
    "MINDFLOCK_RED_ZONE_DIR",
    "MINDFLOCK_TOOL_FEED_DIR",
    "MINDFLOCK_RUN_DIR",
    "MINDFLOCK_TEMPLATES_FILE",
    "MINDFLOCK_DBCLIENT_FILE",
    "MINDFLOCK_WINDOW_REFRESH_FILE",
    "MINDFLOCK_ACTIVITY_MARKER_DIR",
    "MINDFLOCK_THREAD_MARKER_DIR",
    "MINDFLOCK_ASSISTANT_DIR",
)


def _resolvers():
    from backend.config import config, red_zones, settings
    from backend.providers import activity_markers, mcp_attach, thread_markers
    from backend.session import secret_env
    from backend.web.addons import templates
    from backend.web.addons.dbclient import store as dbclient_store
    from backend.web.core import (
        autopilot,
        mailbox,
        ports,
        prompt_queue,
        team_runs,
        test_plans,
        window_refresh,
    )

    return {
        "config dir": config.GetConfigDir,
        "settings": settings.settings_path,
        "ports": ports._path,
        "test plans": test_plans.store_path,
        "autopilot": autopilot.autopilot_path,
        "prompt queue": prompt_queue.queue_path,
        "team runs": team_runs.runs_dir,
        "mailbox": mailbox.mailbox_path,
        "red zones": red_zones.store_path,
        "red-zone guard dir": red_zones.guard_dir,
        "tool feed": red_zones.feed_dir,
        "mcp run dir": mcp_attach.run_dir,
        "secret env": secret_env.run_dir,
        "templates": templates.templates_path,
        "dbclient": dbclient_store.store_path,
        "window refresh": window_refresh.config_path,
        "activity markers": activity_markers.marker_dir,
        "thread markers": thread_markers.marker_dir,
    }


@pytest.fixture
def real_home(monkeypatch):
    """Undo every conftest redirect and point $HOME at the real home — the
    exact environment a scratch conftest without the redirects runs in."""
    for var in _REDIRECTS:
        monkeypatch.delenv(var, raising=False)
    home = home_guard.real_home()
    monkeypatch.setenv("HOME", home)
    return home


@pytest.mark.parametrize("name", sorted(_resolvers()))
def test_every_store_refuses_the_real_home_with_no_redirect(real_home, name):
    with pytest.raises(RealHomeStoreError, match="under pytest"):
        _resolvers()[name]()


@pytest.mark.parametrize(
    "var,leaf",
    [
        ("MINDFLOCK_AUTOPILOT_FILE", ".mindflock/autopilot.json"),
        ("MINDFLOCK_RUNS_DIR", ".mindflock/runs"),
        ("MINDFLOCK_PROMPT_QUEUE_FILE", ".mindflock/prompt_queues.json"),
        ("MINDFLOCK_MAILBOX_FILE", ".mindflock/mailbox.json"),
        ("MINDFLOCK_SETTINGS_FILE", ".mindflock/settings.json"),
        ("MINDFLOCK_RUN_DIR", ".mindflock/run"),
    ],
)
def test_an_override_pointing_at_the_real_store_is_refused_too(
    monkeypatch, tmp_path, var, leaf
):
    """A redirect that points back at the live file is not a redirect."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(var, os.path.join(home_guard.real_home(), leaf))
    from backend.config import settings
    from backend.providers import mcp_attach
    from backend.web.core import autopilot, mailbox, prompt_queue, team_runs

    fn = {
        "MINDFLOCK_AUTOPILOT_FILE": autopilot.autopilot_path,
        "MINDFLOCK_RUNS_DIR": team_runs.runs_dir,
        "MINDFLOCK_PROMPT_QUEUE_FILE": prompt_queue.queue_path,
        "MINDFLOCK_MAILBOX_FILE": mailbox.mailbox_path,
        "MINDFLOCK_SETTINGS_FILE": settings.settings_path,
        "MINDFLOCK_RUN_DIR": mcp_attach.run_dir,
    }[var]
    with pytest.raises(RealHomeStoreError):
        fn()


def test_a_redirected_store_resolves_normally(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from backend.config import config
    from backend.web.core import autopilot

    assert config.GetConfigDir() == str(tmp_path / ".mindflock")
    assert autopilot.autopilot_path().startswith(str(tmp_path))


def test_arming_with_no_redirect_never_writes_the_real_store(real_home):
    """The writer path end to end: the guard fires before any open()."""
    from backend.web.core import autopilot

    before = None
    real = os.path.join(real_home, ".mindflock", "autopilot.json")
    if os.path.exists(real):
        before = os.stat(real).st_mtime_ns
    with pytest.raises(RealHomeStoreError):
        autopilot.arm("home-guard-probe", "pr", source="session")
    after = os.stat(real).st_mtime_ns if os.path.exists(real) else None
    assert after == before


def test_the_ticket_ledger_refuses_the_app_checkout(monkeypatch):
    """The pipeline ledger lives in the app checkout (``config.toml``'s
    directory, which a worktree resolves UP to) — never a test's to write."""
    from backend.ticket_ingestion import state as ledger

    checkout = os.path.dirname(os.path.dirname(os.path.abspath(ledger.__file__)))
    with pytest.raises(RealHomeStoreError, match="ticket ledger"):
        ledger.load_processed_story_statuses(checkout)
    with pytest.raises(RealHomeStoreError):
        ledger.load_processed_story_statuses(os.path.dirname(checkout))


def test_the_ticket_ledger_refuses_another_mindflock_checkout(tmp_path):
    """A sibling checkout (the owner's main one, named by the
    ``MINDFLOCK_REPO_ROOT`` a live server exports into agent shells) is just
    as real as this one: the hand-over record must never land there."""
    from backend.ticket_ingestion import state as ledger

    other = tmp_path / "app"
    (other / "backend" / "ticket_ingestion").mkdir(parents=True)
    with pytest.raises(RealHomeStoreError, match="ticket ledger"):
        ledger.note_automation(other, False, "alpha")
    assert not (other / "automation_here.json").exists()


def test_the_suite_never_inherits_a_real_repo_root():
    """The ingestion controller resolves its repo root at server import, so
    an exported ``MINDFLOCK_REPO_ROOT`` must be gone before any test runs."""
    assert "MINDFLOCK_REPO_ROOT" not in os.environ


def test_the_ledger_in_a_tmp_dir_still_works(tmp_path):
    from backend.ticket_ingestion import state as ledger

    assert ledger.load_processed_story_statuses(tmp_path) == {}


def test_outside_pytest_the_guard_is_the_identity(tmp_path):
    """A real server never imports pytest: the guard must be a no-op there."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "PYTEST_CURRENT_TEST" and not k.startswith("MINDFLOCK_")
    }
    env["HOME"] = home_guard.real_home()
    code = (
        "import sys; from backend.config import config; "
        "assert 'pytest' not in sys.modules; print(config.GetConfigDir())"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == os.path.join(home_guard.real_home(), ".mindflock")
