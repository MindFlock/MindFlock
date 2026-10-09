"""One-click "Install everything missing" (`backend.web.core.setup_install`).

tmux is faked: these pin the lifecycle rules, which are what keep an install
from being killed halfway through an apt run, and the routes' contract.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import doctor
from backend.doctor import Check
from backend.web.core import setup_install


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    """A fake tmux: ``state["session"]`` says whether the session exists, and
    ``state["created"]`` collects the shell commands new-session was given."""
    state = {"session": False, "created": [], "killed": 0}
    monkeypatch.setattr(setup_install, "_run_dir", lambda: str(tmp_path))

    def tmux(*args):
        if args[0] == "has-session":
            return 0 if state["session"] else 1
        if args[0] == "kill-session":
            state["session"] = False
            state["killed"] += 1
        return 0

    class _Done:
        returncode = 0
        stderr = b""

    def run(argv, **kw):
        assert argv[:2] == ["tmux", "new-session"]
        state["session"] = True
        state["created"].append(argv[-1])
        return _Done()

    monkeypatch.setattr(setup_install, "_tmux", tmux)
    monkeypatch.setattr(setup_install, "_have_tmux", lambda: True)
    monkeypatch.setattr(setup_install.subprocess, "run", run)
    monkeypatch.setattr(
        doctor,
        "run_checks",
        lambda: [Check("uv", "uv", "warn", cmd="echo install-uv", install=True)],
    )
    state["dir"] = tmp_path
    return state


def test_starts_the_plan_script_and_records_its_exit(fake):
    session, err = setup_install.ensure_session()
    assert err is None and session == setup_install.SESSION
    script = (fake["dir"] / "setup-install.sh").read_text()
    assert "echo install-uv" in script
    (wrapped,) = fake["created"]
    assert "setup-install.sh" in wrapped
    assert "echo $? > " in wrapped and "setup-install.status" in wrapped
    assert setup_install.state() == {"running": True, "exit_code": None}


def test_nothing_to_install_is_an_error_not_an_empty_terminal(fake, monkeypatch):
    monkeypatch.setattr(doctor, "run_checks", lambda: [Check("uv", "uv", "ok")])
    _, err = setup_install.ensure_session()
    assert err and "nothing to install" in err
    assert fake["created"] == []


def test_a_run_in_progress_is_reattached_not_restarted(fake):
    setup_install.ensure_session()
    setup_install.ensure_session()
    assert len(fake["created"]) == 1


def test_a_finished_run_is_replaced_by_a_fresh_one(fake):
    setup_install.ensure_session()
    (fake["dir"] / "setup-install.status").write_text("1\n")
    assert setup_install.state()["exit_code"] == 1
    setup_install.ensure_session()
    assert len(fake["created"]) == 2 and fake["killed"] == 1
    # The old result is gone, so a UI polling state can't read it as the new
    # run's.
    assert setup_install.exit_code() is None


def test_close_never_kills_an_install_halfway(fake):
    setup_install.ensure_session()
    assert setup_install.close() is False
    assert fake["session"] is True and fake["killed"] == 0
    (fake["dir"] / "setup-install.status").write_text("0\n")
    assert setup_install.close() is True
    assert fake["session"] is False


def test_the_script_is_private_to_the_user(fake):
    setup_install.ensure_session()
    mode = (fake["dir"] / "setup-install.sh").stat().st_mode & 0o777
    assert mode == 0o700


def test_routes(fake):
    from backend.web import server

    c = TestClient(server.app, client=("127.0.0.1", 50000))
    assert c.get("/api/doctor/install-state").json() == {
        "running": False,
        "exit_code": None,
    }
    setup_install.ensure_session()
    assert c.post("/api/doctor/install-close").json() == {"closed": False}
    (fake["dir"] / "setup-install.status").write_text("0\n")
    assert c.get("/api/doctor/install-state").json() == {
        "running": False,
        "exit_code": 0,
    }
    assert c.post("/api/doctor/install-close").json() == {"closed": True}


def test_terminal_reports_why_it_cannot_open(fake, monkeypatch):
    from backend.web import server

    monkeypatch.setattr(doctor, "run_checks", lambda: [])
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    with c.websocket_connect("/api/doctor/install-terminal") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error" and "nothing to install" in msg["message"]


def test_terminal_refuses_a_caller_not_at_this_device(fake, monkeypatch):
    """It runs installers and sudo: an anonymous tailnet caller of an exposed
    gate-off server gets an error, and nothing starts."""
    from backend.web import server
    from backend.web.core import tailnet_trust

    async def _untrusted(scope):
        return False

    monkeypatch.setattr(tailnet_trust, "request_trusted", _untrusted)
    c = TestClient(server.app, client=("100.64.0.9", 50000))
    with c.websocket_connect("/api/doctor/install-terminal") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error" and "only allowed" in msg["message"]
    assert fake["created"] == []


# --------------------------------------------------------------------------- #
# No tmux — the dependency a fresh machine is most likely missing. The script
# runs under a plain PTY with the same contract (exit-status file, survives
# the window closing, replayed on reattach).
# --------------------------------------------------------------------------- #
def _wait(pred, timeout=10.0):
    import time

    deadline = time.time() + timeout
    while not pred() and time.time() < deadline:
        time.sleep(0.05)
    return pred()


def test_without_tmux_the_script_runs_under_a_plain_pty(fake, monkeypatch):
    from backend.web.core import pty_run

    monkeypatch.setattr(setup_install, "_have_tmux", lambda: False)

    def _no_tmux(*a, **kw):
        raise AssertionError("tmux must not be called")

    monkeypatch.setattr(setup_install, "_tmux", _no_tmux)
    try:
        session, err = setup_install.ensure_session()
        assert err is None and session == setup_install.SESSION
        assert fake["created"] == []  # never `tmux new-session`
        run = pty_run.get(session)
        assert run is not None
        assert _wait(lambda: setup_install.exit_code() is not None)
        assert setup_install.exit_code() == 0
        assert _wait(lambda: not run.alive())
        out = run.attach(lambda _d: None)
        assert b"install-uv" in out and b"you can close this window" in out
        # No interactive shell is left behind in a plain PTY.
        assert setup_install.state() == {"running": False, "exit_code": 0}
        assert setup_install.close() is True
        assert pty_run.get(session) is None
    finally:
        pty_run.kill(setup_install.SESSION)


def test_without_tmux_a_run_in_progress_is_reattached(fake, monkeypatch):
    from backend.web.core import pty_run

    monkeypatch.setattr(setup_install, "_have_tmux", lambda: False)
    monkeypatch.setattr(
        doctor,
        "run_checks",
        lambda: [Check("uv", "uv", "warn", cmd="sleep 30", install=True)],
    )
    try:
        setup_install.ensure_session()
        first = pty_run.get(setup_install.SESSION)
        setup_install.ensure_session()
        assert pty_run.get(setup_install.SESSION) is first
        assert setup_install.state() == {"running": True, "exit_code": None}
        # Closing the window mid-install never kills it.
        assert setup_install.close() is False and first.alive()
    finally:
        pty_run.kill(setup_install.SESSION)


def test_without_tmux_the_browser_terminal_streams_the_run(fake, monkeypatch):
    from backend.web import server
    from backend.web.core import pty_run

    monkeypatch.setattr(setup_install, "_have_tmux", lambda: False)
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    try:
        seen = b""
        with c.websocket_connect("/api/doctor/install-terminal") as ws:
            while b"you can close this window" not in seen:
                seen += ws.receive_bytes()
        assert b"install-uv" in seen
        # Detaching left the (finished) run in place for a reattach to replay.
        assert pty_run.get(setup_install.SESSION) is not None
    finally:
        pty_run.kill(setup_install.SESSION)
