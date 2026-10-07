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

    c = TestClient(server.app)
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
    c = TestClient(server.app)
    with c.websocket_connect("/api/doctor/install-terminal") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error" and "nothing to install" in msg["message"]
