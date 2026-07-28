"""O2/O3 server endpoints + push soft gate + held-prompt drain gate.

Hermetic in the test_prompt_queue.py style: a fake instance whose worktree is
a tmp dir is dropped into ``server.ENGINE.instances``; no tmux is spawned.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from backend.session.storage import GitWorktreeData, InstanceData, Status
from backend.web import server
from backend.web.core import worktree_setup as ws


def _wait(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _mk_inst(title, wt):
    from backend.session.instance import FromInstanceData

    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=120)
    data = InstanceData(
        title=title,
        path=wt,
        branch="b",
        status=Status.Running,
        created_at=t,
        updated_at=t,
        program="bash",
        worktree=GitWorktreeData(
            repo_path=wt,
            worktree_path=wt,
            session_name=title,
            branch_name="b",
        ),
    )
    return FromInstanceData(data, attach=False)


@pytest.fixture
def wt(tmp_path):
    """A real git repo standing in for the session's worktree."""
    r = tmp_path / "wt"
    r.mkdir()
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=r, check=True)
    (r / "a.txt").write_text("a")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)
    return r


@pytest.fixture
def client(wt, monkeypatch):
    inst = _mk_inst("t1", str(wt))
    monkeypatch.setitem(server.ENGINE.instances, "t1", inst)
    c = TestClient(server.app)
    yield c
    server.ENGINE.instances.pop("t1", None)


# --------------------------------------------------------------------------- #
# Setup + check endpoints
# --------------------------------------------------------------------------- #
def test_setup_endpoints(client, wt):
    (wt / ws.CONFIG_NAME).write_text(
        '[workspace]\nsetup_commands = ["echo made > out.txt"]\n'
    )
    r = client.post("/api/instances/t1/setup/rerun")
    assert r.status_code == 202
    assert _wait(lambda: (ws.setup_status(str(wt)) or {}).get("state") == "ok")
    got = client.get("/api/instances/t1/setup").json()
    assert got["status"]["state"] == "ok"
    assert (wt / "out.txt").exists()


def test_setup_rerun_without_config_is_400(client):
    assert client.post("/api/instances/t1/setup/rerun").status_code == 400


def test_setup_unknown_session_is_404(client):
    assert client.get("/api/instances/nope/setup").status_code == 404


def test_check_endpoints(client, wt):
    (wt / ws.CONFIG_NAME).write_text('[workspace]\ncheck_command = "true"\n')
    r = client.post("/api/instances/t1/check")
    assert r.status_code == 202
    assert _wait(lambda: (ws.check_status(str(wt)) or {}).get("state") == "ok")
    got = client.get("/api/instances/t1/check").json()
    assert got["status"]["state"] == "ok" and got["status"]["stale"] is False


def test_check_run_without_config_is_400(client):
    assert client.post("/api/instances/t1/check").status_code == 400


# --------------------------------------------------------------------------- #
# Push soft gate (O3)
# --------------------------------------------------------------------------- #
def _no_shell_push(monkeypatch):
    """Neuter the real shell-push machinery; report whether it was reached."""
    reached = []
    monkeypatch.setattr(server, "_ensure_shell_session", lambda t, w: ("sh", None))
    monkeypatch.setattr(server, "_send_to_shell", lambda n, c: reached.append(c))
    monkeypatch.setattr(server, "_has_origin", lambda w, fresh=False: True)
    return reached


def test_push_gated_when_no_check_result(client, wt, monkeypatch):
    reached = _no_shell_push(monkeypatch)
    (wt / ws.CONFIG_NAME).write_text('[workspace]\ncheck_command = "true"\n')
    r = client.post("/api/instances/t1/push-branch", json={})
    assert r.status_code == 409 and r.json()["check_required"] is True
    assert reached == []


def test_push_force_overrides_gate(client, wt, monkeypatch):
    reached = _no_shell_push(monkeypatch)
    (wt / ws.CONFIG_NAME).write_text('[workspace]\ncheck_command = "true"\n')
    r = client.post("/api/instances/t1/push-branch", json={"force": True})
    assert r.status_code == 200
    assert reached and "git push" in reached[0]


def test_push_allowed_after_passing_check(client, wt, monkeypatch):
    reached = _no_shell_push(monkeypatch)
    (wt / ws.CONFIG_NAME).write_text('[workspace]\ncheck_command = "true"\n')
    client.post("/api/instances/t1/check")
    assert _wait(lambda: (ws.check_status(str(wt)) or {}).get("state") == "ok")
    r = client.post("/api/instances/t1/push-branch", json={})
    assert r.status_code == 200 and reached


def test_push_ungated_without_check_command(client, wt, monkeypatch):
    reached = _no_shell_push(monkeypatch)
    r = client.post("/api/instances/t1/push-branch", json={})
    assert r.status_code == 200 and reached


# --------------------------------------------------------------------------- #
# Drain gate (O2): queued prompts held while setup runs / after it fails
# --------------------------------------------------------------------------- #
def test_drain_holds_prompts_during_and_after_failed_setup(
    client, wt, monkeypatch, tmp_path
):
    from backend.web.core import prompt_queue as pq

    monkeypatch.setenv("MINDFLOCK_PROMPT_QUEUE_FILE", str(tmp_path / "q.json"))
    sent = []
    monkeypatch.setattr(server, "_ensure_agent_session", lambda i, t: ("a", None))
    monkeypatch.setattr(
        server, "_send_to_agent", lambda n, x, submit=True: sent.append(x) or True
    )
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    pq.enqueue("t1", "held prompt")
    pq.set_flags("t1", enabled=True)

    ws._write_status(str(wt), ws.SETUP_STATUS, {"state": "running"})
    server._drain_one_queue("t1")
    assert sent == []  # held while setup runs
    ws._write_status(str(wt), ws.SETUP_STATUS, {"state": "failed", "rc": 1})
    server._drain_one_queue("t1")
    assert sent == []  # held after a failed setup (#2847)
    ws._write_status(str(wt), ws.SETUP_STATUS, {"state": "ok", "rc": 0})
    # The setup gate returns before the idle-dwell logic, so those held passes
    # never started the dwell timer. This test is about the setup gate, not the
    # dwell — pre-settle idle so the release path sends immediately.
    server._QUEUE_STATE["t1"] = {
        "armed": True,
        "sent_at": 0.0,
        "rebooted_at": 0.0,
        "idle_since": 1.0,
    }
    server._drain_one_queue("t1")
    assert sent == ["held prompt"]  # released once setup is ok
    server._QUEUE_STATE.pop("t1", None)
