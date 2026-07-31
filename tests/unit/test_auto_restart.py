"""Restarting the server to make tailscale mode real — and knowing when to stop.

Which interface uvicorn binds is decided once, at boot, so the Settings → Mobile
toggle is inert until the process restarts. :mod:`backend.web.core.restart` does
that restart itself rather than leaving a button to press, which makes two things
load-bearing:

* it must **never** fire in a process that merely imported the app (a test run
  replaced mid-suite by a web server is not a failure anyone can debug), and
* it must stop. A restart that doesn't change the outcome would otherwise
  restart forever, so the attempt count rides across ``execv`` in the
  environment — the only place an in-memory counter couldn't survive to reach
  its own limit.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import restart


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""


@pytest.fixture(autouse=True)
def _no_execv(monkeypatch):
    """Record re-exec requests instead of performing them, and start every test
    with a clean attempt budget."""
    calls: list = []
    monkeypatch.setattr(restart, "reexec_soon", lambda delay=0.5: calls.append(delay))
    monkeypatch.delenv(restart._ATTEMPT_ENV, raising=False)
    return calls


@pytest.fixture
def execs(_no_execv):
    return _no_execv


@pytest.fixture
def armed(monkeypatch):
    """The conditions under which the auto-restart is *supposed* to fire: this
    process is the server, tailscale mode is on, and the bind is still local."""
    monkeypatch.setattr(restart, "_SERVING", True)
    monkeypatch.setattr(restart, "_under_pytest", lambda: False)
    monkeypatch.setenv("CS_WEB_MODE", "local")
    S.update_settings(general={"serve_mode": "tailscale"})


# --------------------------------------------------------------------------- #
# The guards: who is allowed to replace this process
# --------------------------------------------------------------------------- #
def test_a_test_run_is_never_re_execed(monkeypatch, execs):
    """The default state of this very suite: every precondition met except the
    one that matters."""
    monkeypatch.setattr(restart, "_SERVING", True)
    monkeypatch.setenv("CS_WEB_MODE", "local")
    S.update_settings(general={"serve_mode": "tailscale"})
    assert restart.auto_restart_for_tailscale() is False
    assert execs == []


def test_an_imported_app_is_never_re_execed(monkeypatch, execs):
    """Without run.main's marker this process is a script, a REPL or a test —
    none of which asked to become a server."""
    monkeypatch.setattr(restart, "_under_pytest", lambda: False)
    monkeypatch.setattr(restart, "_SERVING", False)
    monkeypatch.setenv("CS_WEB_MODE", "local")
    S.update_settings(general={"serve_mode": "tailscale"})
    assert restart.auto_restart_for_tailscale() is False
    assert execs == []


def test_reexec_soon_is_inert_under_pytest(monkeypatch):
    """The guard sits in the low-level call too — one missed caller shouldn't be
    able to take the suite down with it."""
    spawned: list = []
    monkeypatch.setattr(
        restart.threading, "Thread", lambda *a, **kw: spawned.append(kw) or _Dummy()
    )
    restart.reexec_soon()
    assert spawned == []


class _Dummy:
    def start(self):  # pragma: no cover — never reached
        raise AssertionError("a re-exec thread was started under pytest")


# --------------------------------------------------------------------------- #
# The conditions
# --------------------------------------------------------------------------- #
def test_no_restart_when_tailscale_mode_is_off(armed, execs):
    S.update_settings(general={"serve_mode": "local"})
    assert restart.auto_restart_for_tailscale() is False
    assert execs == []


def test_no_restart_when_the_mode_is_already_in_effect(monkeypatch, armed, execs):
    """Bound to 0.0.0.0 already — there is nothing to apply."""
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    assert restart.auto_restart_for_tailscale() is False
    assert execs == []


def test_no_restart_when_the_bind_is_unknown(monkeypatch, armed, execs):
    """A bare uvicorn leaves CS_WEB_MODE unset; guessing it is local and
    re-execing would be acting on nothing."""
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    assert restart.auto_restart_for_tailscale() is False
    assert execs == []


def test_restarts_when_the_setting_is_not_in_effect(armed, execs):
    assert restart.auto_restart_for_tailscale() is True
    assert execs == [1.0]


def test_the_caller_can_shorten_the_grace_period(armed, execs):
    """The settings route has a client waiting on a response, not a fresh
    server finding its feet."""
    assert restart.auto_restart_for_tailscale(delay=0.5) is True
    assert execs == [0.5]


# --------------------------------------------------------------------------- #
# The budget
# --------------------------------------------------------------------------- #
def test_attempts_are_counted_in_the_environment(armed, execs):
    """execv keeps the environment, so this is the counter that survives the
    restart it is counting."""
    restart.auto_restart_for_tailscale()
    import os

    assert os.environ[restart._ATTEMPT_ENV] == "1"


def test_it_gives_up_after_three_attempts(armed, execs):
    for expected in range(1, restart.MAX_TAILSCALE_ATTEMPTS + 1):
        assert restart.auto_restart_for_tailscale() is True
        assert len(execs) == expected
    assert restart.auto_restart_for_tailscale() is False
    assert len(execs) == restart.MAX_TAILSCALE_ATTEMPTS


def test_a_spent_budget_stays_spent(armed, execs, monkeypatch):
    monkeypatch.setenv(restart._ATTEMPT_ENV, str(restart.MAX_TAILSCALE_ATTEMPTS))
    assert restart.auto_restart_for_tailscale() is False
    assert execs == []


def test_a_garbled_counter_is_treated_as_zero(armed, execs, monkeypatch):
    monkeypatch.setenv(restart._ATTEMPT_ENV, "not-a-number")
    assert restart.auto_restart_for_tailscale() is True


def test_resetting_hands_the_budget_back(armed, execs):
    """What an explicit user action (the toggle, a manual restart) does: this
    intent is new, and shouldn't inherit the last one's give-up."""
    for _ in range(restart.MAX_TAILSCALE_ATTEMPTS):
        restart.auto_restart_for_tailscale()
    assert restart.auto_restart_for_tailscale() is False
    restart.reset_tailscale_attempts()
    assert restart.auto_restart_for_tailscale() is True


# --------------------------------------------------------------------------- #
# The routes that drive it
# --------------------------------------------------------------------------- #
# Loopback base URL: these tests run with CS_WEB_MODE=local, where the
# DNS-rebinding guard (auth.host_ok) refuses any Host that isn't loopback —
# including TestClient's default "testserver".
client = TestClient(server.app, base_url="http://127.0.0.1")


def test_the_toggle_restarts_and_says_so(armed, execs):
    """Settings → Mobile flips the switch; the server takes the restart itself
    and tells the client to wait for it rather than report the dropped
    connection as a failure."""
    S.update_settings(general={"serve_mode": "local"})
    r = client.post("/api/settings", json={"general": {"serve_mode": "tailscale"}})
    assert r.status_code == 200
    assert r.json()["restarting"] is True
    assert execs == [0.5]


def test_turning_the_toggle_off_restarts_nothing(armed, execs):
    r = client.post("/api/settings", json={"general": {"serve_mode": "local"}})
    assert r.status_code == 200
    assert "restarting" not in r.json()
    assert execs == []


def test_the_toggle_hands_back_a_spent_budget(armed, execs, monkeypatch):
    """A user who flips the switch after the automatic retries gave up gets a
    restart, not silence."""
    S.update_settings(general={"serve_mode": "local"})
    monkeypatch.setenv(restart._ATTEMPT_ENV, str(restart.MAX_TAILSCALE_ATTEMPTS))
    r = client.post("/api/settings", json={"general": {"serve_mode": "tailscale"}})
    assert r.json()["restarting"] is True
    assert execs == [0.5]


def test_the_manual_restart_endpoint_still_re_execs(execs, monkeypatch):
    monkeypatch.setenv(restart._ATTEMPT_ENV, "2")
    r = client.post("/api/server/restart")
    assert r.status_code == 200 and r.json()["restarting"] is True
    assert execs == [0.5]
    # Explicit intent: the automatic retries start over after it.
    import os

    assert restart._ATTEMPT_ENV not in os.environ
