"""Update all my devices (core.fleet_update + the fleet addon's routes).

The receiver is the dangerous half — it reinstalls the engine on request — so
it answers only the fleet key and installs only a published release at or
above what it runs. The rollout's promises: one member at a time, done only
when that member's OWN hello reports the new build, the first failure stops
everything after it, and a member that can't be updated from here (a dev
checkout, an old MindFlock, offline) is skipped with the reason instead.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.web.addons.fleet import FleetAddon
from backend.web.core import fleet, fleet_update, remote
from backend.web.core import self_update

KEY = "fleet-key-XYZ_0123456789"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_update, "_path", lambda: tmp_path / "fleet_update.json")
    # THIS device's own update.json (its row follows it): never the real one.
    monkeypatch.setattr(self_update, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(fleet_update, "_TASK", {"task": None})
    monkeypatch.setattr(fleet_update, "POLL_S", 0.0)
    monkeypatch.setattr(fleet, "in_fleet", lambda: True)
    monkeypatch.setattr(fleet, "fleet_key", lambda: KEY)
    monkeypatch.setattr(fleet, "key_valid", lambda c: c == KEY)
    monkeypatch.setattr(fleet, "_self_key", lambda: "laptop")
    monkeypatch.setattr(fleet, "_self_host", lambda: "Laptop")
    monkeypatch.setattr(fleet, "member_device", lambda dev: True)
    monkeypatch.setattr(self_update, "installed_version", lambda: "9.9.9")
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")

    async def _ok(ref):
        return "", 200

    monkeypatch.setattr(self_update, "check_remote_ref", _ok)


class _Fleet:
    """Members on a fake tailnet: each has a hello (version/commit/install)
    and answers apply/state like a real member would."""

    def __init__(self, monkeypatch, members):
        self.devs = {
            k: {
                "key": k,
                "host": k.title(),
                "reachable": True,
                "version": "0.7.4",
                "commit": "0" * 40,
                "install": "uv-tool",
                "shell_version": "",
                **v,
            }
            for k, v in members.items()
        }
        self.log = []  # ("apply"|"state"|"probe", key)
        self.on_apply = {}  # key -> (status, body)
        self.boots = {}  # key -> {"after": n probes, "commit": ...} ; None = never
        self.state = {}  # key -> member update state
        self.probes = {}
        live = {k: {"host": d["host"]} for k, d in self.devs.items()}
        live["laptop"] = {"host": "Laptop"}
        monkeypatch.setattr(fleet, "live_members", lambda: live)

        async def _probe(key):
            self.log.append(("probe", key))
            self.probes[key] = self.probes.get(key, 0) + 1
            boot = self.boots.get(key)
            if boot and self.probes[key] > boot["after"]:
                self.devs[key].update(version="9.9.9", commit=boot["commit"])
            return dict(self.devs[key])

        async def _post(dev, path, body, timeout=10.0, *, auth=True, bearer=None):
            assert path == "/api/fleet/update/apply" and bearer == KEY
            self.log.append(("apply", dev["key"]))
            return self.on_apply.get(
                dev["key"], (200, {"ok": True, "ref": "v9.9.9", "commit": "a" * 40})
            )

        async def _get(dev, path, timeout=3.0, *, auth=True, bearer=None):
            assert path == "/api/fleet/update/state" and bearer == KEY
            self.log.append(("state", dev["key"]))
            return 200, self.state.get(dev["key"], {"state": "started"})

        monkeypatch.setattr(fleet_update, "_probe", _probe)
        monkeypatch.setattr(remote, "post_json", _post)
        monkeypatch.setattr(remote, "get_json", _get)


async def _rollout(tag="v9.9.9"):
    out, status = await fleet_update.start(tag)
    assert status == 200, out
    await fleet_update._TASK["task"]
    return fleet_update.status()


def _steps(doc):
    return {r["key"]: r["step"] for r in doc["members"]}


@pytest.mark.asyncio
async def test_members_update_one_at_a_time_and_each_must_come_back(monkeypatch):
    f = _Fleet(monkeypatch, {"mini": {}, "rig": {}})
    f.boots = {
        "mini": {"after": 3, "commit": "a" * 40},
        "rig": {"after": 3, "commit": "a" * 40},
    }
    doc = await _rollout()
    assert doc["state"] == "done"
    assert _steps(doc) == {"mini": "done", "rig": "done", "laptop": "current"}
    applies = [k for kind, k in f.log if kind == "apply"]
    assert applies == ["mini", "rig"]
    # rig was not asked until mini's own hello reported the new build.
    first_rig = f.log.index(("apply", "rig"))
    assert f.probes and f.log[:first_rig].count(("probe", "mini")) > 3


@pytest.mark.asyncio
async def test_the_rollout_halts_on_the_first_member_that_never_comes_back(
    monkeypatch,
):
    monkeypatch.setattr(fleet_update, "MEMBER_TIMEOUT_S", 0.05)
    f = _Fleet(monkeypatch, {"mini": {}, "rig": {}})
    f.boots = {"rig": {"after": 1, "commit": "a" * 40}}  # mini never boots
    doc = await _rollout()
    assert doc["state"] == "halted"
    assert _steps(doc)["mini"] == "failed"
    assert _steps(doc)["rig"] == "not_started"
    assert ("apply", "rig") not in f.log
    assert doc["error"].startswith("Mini:")


@pytest.mark.asyncio
async def test_a_member_that_rolled_back_stops_the_rollout(monkeypatch):
    f = _Fleet(monkeypatch, {"mini": {}, "rig": {}})
    f.state = {"mini": {"state": "rolled_back"}}
    doc = await _rollout()
    assert doc["state"] == "halted"
    assert _steps(doc) == {
        "mini": "failed",
        "rig": "not_started",
        "laptop": "not_started",
    }
    assert (
        "rolled back" in [r for r in doc["members"] if r["key"] == "mini"][0]["detail"]
    )


@pytest.mark.asyncio
async def test_installed_but_never_restarted_fails_after_the_grace(monkeypatch):
    monkeypatch.setattr(fleet_update, "RESTART_GRACE_S", 0.0)
    f = _Fleet(monkeypatch, {"mini": {}})
    f.state = {"mini": {"state": "done"}}
    doc = await _rollout()
    assert doc["state"] == "halted"
    row = doc["members"][0]
    assert row["step"] == "failed" and "didn't come back" in row["detail"]


@pytest.mark.asyncio
async def test_blocked_offline_old_and_current_members_are_skipped_not_fatal(
    monkeypatch,
):
    f = _Fleet(
        monkeypatch,
        {
            "dev": {"install": "editable"},
            "gone": {"reachable": False},
            "old": {},
            "same": {"version": "9.9.9"},
            "rig": {},
        },
    )
    f.on_apply = {"old": (404, None)}
    f.boots = {"rig": {"after": 1, "commit": "a" * 40}}
    doc = await _rollout()
    assert doc["state"] == "done"
    steps = _steps(doc)
    assert steps["dev"] == "skipped" and steps["gone"] == "skipped"
    assert steps["old"] == "skipped" and steps["same"] == "current"
    assert steps["rig"] == "done"
    detail = {r["key"]: r["detail"] for r in doc["members"]}
    assert "dev checkout" in detail["dev"] and detail["gone"] == "offline"
    assert "too old" in detail["old"]
    # The fleet key never went to the blocked or offline ones.
    assert ("apply", "dev") not in f.log and ("apply", "gone") not in f.log


@pytest.mark.asyncio
async def test_a_member_that_answers_blocked_is_skipped(monkeypatch):
    f = _Fleet(monkeypatch, {"mini": {}})
    f.on_apply = {"mini": (409, {"ok": False, "blocked": True, "install": "other"})}
    doc = await _rollout()
    assert doc["state"] == "done" and _steps(doc)["mini"] == "skipped"
    assert "install.sh" in doc["members"][0]["detail"]


@pytest.mark.asyncio
async def test_this_device_updates_itself_last(monkeypatch):
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    order = []
    f = _Fleet(monkeypatch, {"mini": {}})
    f.boots = {"mini": {"after": 1, "commit": "a" * 40}}
    real_post = remote.post_json

    async def _post(dev, path, body, **kw):
        order.append("mini")
        return await real_post(dev, path, body, **kw)

    monkeypatch.setattr(remote, "post_json", _post)
    monkeypatch.setattr(self_update, "installed_commit", lambda: "0" * 40)

    def _start(tag):
        order.append("self")
        self_update.write_state(state="started", ref=tag, commit="a" * 40)
        return {"ok": True, "commit": "a" * 40}

    monkeypatch.setattr(self_update, "start_update", _start)
    out, status = await fleet_update.start("v9.9.9")
    assert status == 200
    task = fleet_update._TASK["task"]

    def me():
        return [r for r in fleet_update.status()["members"] if r["self"]][0]

    for _ in range(200):
        await asyncio.sleep(0)
        if order == ["mini", "self"]:
            break
    assert order == ["mini", "self"]
    # Installing: the rollout is NOT done yet — it is this device's turn.
    await asyncio.sleep(0.01)
    assert fleet_update.status()["state"] == "running"
    assert me()["step"] == "updating"
    # Installed: restarting onto it.
    self_update.write_state(
        state="done",
        ref="v9.9.9",
        commit="a" * 40,
        finished_at=__import__("time").time(),
    )
    for _ in range(200):
        await asyncio.sleep(0.001)
        if me()["step"] == "restarting":
            break
    assert me()["step"] == "restarting"
    assert fleet_update.status()["state"] == "running"
    # The watcher re-execs this process: the task dies with it …
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # … and the new process, running the installed build, finishes the rollout.
    monkeypatch.setattr(self_update, "installed_commit", lambda: "a" * 40)
    doc = fleet_update.status()
    assert doc["state"] == "done" and me()["step"] == "done"


@pytest.mark.asyncio
async def test_this_device_failing_its_own_install_halts_the_rollout(monkeypatch):
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    _Fleet(monkeypatch, {})

    def _start(tag):
        self_update.write_state(state="failed", ref=tag, error="uv exploded", code=2)
        return {"ok": True, "commit": "a" * 40}

    monkeypatch.setattr(self_update, "start_update", _start)
    doc = await _rollout()
    assert doc["state"] == "halted" and doc["error"] == "this device: uv exploded"
    assert _steps(doc)["laptop"] == "failed"


def test_a_rollback_here_after_the_restart_reads_as_failed(monkeypatch):
    """The new build didn't boot; the installer put the old one back and
    relaunched it. The relaunched server says so on this device's row."""
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(self_update, "installed_commit", lambda: "b" * 40)
    fleet_update._write(
        {
            "state": "running",
            "tag": "v9.9.9",
            "version": "9.9.9",
            "error": "",
            "members": [
                {"key": "mini", "host": "Mini", "step": "done"},
                {"key": "laptop", "host": "Laptop", "self": True, "step": "restarting"},
            ],
        }
    )
    self_update.write_state(state="rolled_back", ref="v9.9.9", commit="a" * 40)
    doc = fleet_update.status()
    assert doc["state"] == "halted"
    assert _steps(doc) == {"mini": "done", "laptop": "failed"}
    assert "rolled back" in doc["error"]


def test_this_devices_install_still_running_after_a_restart_stays_running(
    monkeypatch,
):
    import time

    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(self_update, "installed_commit", lambda: "0" * 40)
    fleet_update._write(
        {
            "state": "running",
            "tag": "v9.9.9",
            "version": "9.9.9",
            "error": "",
            "members": [
                {"key": "laptop", "host": "Laptop", "self": True, "step": "restarting"},
            ],
        }
    )
    # Installed moments ago, the restart onto it not landed yet.
    self_update.write_state(
        state="done", ref="v9.9.9", commit="a" * 40, finished_at=time.time()
    )
    assert fleet_update.status()["state"] == "running"
    # Long past the grace with no restart: it didn't come back.
    self_update.write_state(
        state="done",
        ref="v9.9.9",
        commit="a" * 40,
        finished_at=time.time() - fleet_update.RESTART_GRACE_S - 1,
    )
    doc = fleet_update.status()
    assert doc["state"] == "halted" and _steps(doc) == {"laptop": "failed"}


@pytest.mark.asyncio
async def test_the_fleet_key_is_re_checked_before_every_state_request(monkeypatch):
    """A member that stops matching its recorded name mid-update (another
    node answering for it while it restarts) never gets the key."""
    monkeypatch.setattr(fleet_update, "MEMBER_TIMEOUT_S", 0.05)
    f = _Fleet(monkeypatch, {"mini": {}})
    checks = {"n": 0}

    def _member(dev):
        checks["n"] += 1
        return checks["n"] == 1  # the pre-apply check only

    monkeypatch.setattr(fleet, "member_device", _member)
    doc = await _rollout()
    assert ("apply", "mini") in f.log
    assert ("state", "mini") not in f.log
    assert checks["n"] > 1 and _steps(doc)["mini"] == "failed"


@pytest.mark.asyncio
async def test_a_desktop_shell_behind_says_it_updates_on_next_launch(monkeypatch):
    f = _Fleet(monkeypatch, {"mac": {"shell_version": "0.7.4"}})
    f.boots = {"mac": {"after": 1, "commit": "a" * 40}}
    doc = await _rollout()
    row = doc["members"][0]
    assert row["step"] == "done"
    assert row["detail"] == "desktop app on Mac (v0.7.4) updates on its next launch"


@pytest.mark.asyncio
async def test_a_hello_without_the_shell_version_keeps_the_last_one(monkeypatch):
    """Right after its restart a member's hello may not know its desktop app
    yet ("") — the row keeps what it said before."""
    f = _Fleet(monkeypatch, {"mac": {"shell_version": "0.7.4"}})
    f.boots = {"mac": {"after": 1, "commit": "a" * 40}}
    real = fleet_update._probe

    async def _probe(key):
        dev = await real(key)
        if dev.get("version") == "9.9.9":
            dev["shell_version"] = ""
        return dev

    monkeypatch.setattr(fleet_update, "_probe", _probe)
    doc = await _rollout()
    row = doc["members"][0]
    assert row["step"] == "done" and row["shell_version"] == "0.7.4"
    assert "updates on its next launch" in row["detail"]


@pytest.mark.asyncio
async def test_a_second_rollout_is_refused_while_one_runs(monkeypatch):
    f = _Fleet(monkeypatch, {"mini": {}})
    f.boots = {"mini": {"after": 50, "commit": "a" * 40}}
    first, status = await fleet_update.start("v9.9.9")
    assert status == 200
    again, status = await fleet_update.start("v9.9.9")
    assert status == 409
    await fleet_update._TASK["task"]


def test_a_rollout_cut_off_by_a_restart_reads_as_halted(tmp_path):
    fleet_update._write(
        {
            "state": "running",
            "tag": "v9.9.9",
            "version": "9.9.9",
            "error": "",
            "members": [
                {"key": "mini", "host": "Mini", "step": "updating"},
                {"key": "rig", "host": "Rig", "step": "queued"},
            ],
        }
    )
    doc = fleet_update.status()
    assert doc["state"] == "halted" and "restarted" in doc["error"]
    assert _steps(doc) == {"mini": "failed", "rig": "not_started"}


# --------------------------------------------------------------------------- #
# The receiver
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_apply_refuses_what_check_remote_ref_refuses(monkeypatch):
    async def _no(ref):
        return "only a release tag", 400

    monkeypatch.setattr(self_update, "check_remote_ref", _no)
    monkeypatch.setattr(
        self_update, "start_update", lambda tag: pytest.fail("installed a bad ref")
    )
    out, status = await fleet_update.apply({"tag": "main"})
    assert status == 400 and out["ok"] is False


@pytest.mark.asyncio
async def test_apply_on_a_dev_checkout_answers_blocked(monkeypatch):
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "dev checkout")
    monkeypatch.setattr(self_update, "install_kind", lambda: "editable")
    out, status = await fleet_update.apply({"tag": "v9.9.9"})
    assert status == 409 and out["blocked"] is True and out["install"] == "editable"


@pytest.mark.asyncio
async def test_apply_defaults_to_the_newest_release(monkeypatch):
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")

    async def _latest(force=False):
        return {"tag": "v9.9.9", "version": "9.9.9"}

    monkeypatch.setattr(self_update, "latest_release", _latest)
    seen = []
    monkeypatch.setattr(
        self_update,
        "start_update",
        lambda tag: seen.append(tag) or {"ok": True, "ref": tag, "commit": "a" * 40},
    )
    out, status = await fleet_update.apply({})
    assert status == 200 and seen == ["v9.9.9"] and out["commit"] == "a" * 40


@pytest.mark.asyncio
async def test_apply_when_already_current_installs_nothing(monkeypatch):
    monkeypatch.setattr(
        self_update, "start_update", lambda tag: pytest.fail("reinstalled")
    )
    out, status = await fleet_update.apply({"tag": "v9.9.9"})
    assert status == 200 and out["current"] is True


# --------------------------------------------------------------------------- #
# Who may call what
# --------------------------------------------------------------------------- #
@pytest.fixture()
def http():
    app = FastAPI()
    app.include_router(FleetAddon().router)
    return lambda **kw: TestClient(app, **kw)


def test_the_receiver_answers_only_the_fleet_key(http, monkeypatch):
    monkeypatch.setattr(fleet, "unauthorized_body", lambda: {"error": "not a member"})

    async def _apply(body):
        return {"ok": True}, 200

    monkeypatch.setattr(fleet_update, "apply", _apply)
    c = http(
        client=("127.0.0.1", 50000), headers={"host": "127.0.0.1"}
    )  # even from this machine
    assert c.post("/api/fleet/update/apply", json={}).status_code == 401
    assert (
        c.post(
            "/api/fleet/update/apply",
            json={},
            headers={"Authorization": "Bearer someone-elses-token"},
        ).status_code
        == 401
    )
    assert c.get("/api/fleet/update/state").status_code == 401
    ok = c.post(
        "/api/fleet/update/apply", json={}, headers={"Authorization": "Bearer " + KEY}
    )
    assert ok.status_code == 200


def test_the_origin_route_requires_privileged(http, monkeypatch):
    async def _start(tag=""):
        pytest.fail("an unprivileged caller started a fleet update")

    monkeypatch.setattr(fleet_update, "start", _start)
    # An anonymous tailnet caller …
    r = http(client=("100.64.0.9", 4321)).post("/api/fleet/update", json={})
    assert r.status_code == 403
    # … and another MindFlock relaying, even with the fleet key.
    r = http(client=("127.0.0.1", 50000), headers={"host": "127.0.0.1"}).post(
        "/api/fleet/update",
        json={},
        headers={"X-MindFlock-Remote": "rig", "Authorization": "Bearer " + KEY},
    )
    assert r.status_code == 403


def test_the_origin_route_starts_a_rollout_from_this_machine(http, monkeypatch):
    seen = []

    async def _start(tag=""):
        seen.append(tag)
        return {"state": "running", "members": []}, 200

    monkeypatch.setattr(fleet_update, "start", _start)
    r = http(client=("127.0.0.1", 50000), headers={"host": "127.0.0.1"}).post(
        "/api/fleet/update", json={"tag": "v9.9.9"}
    )
    assert r.status_code == 200 and seen == ["v9.9.9"]


def test_update_routes_stay_off_the_forwarding_allow_list():
    """The browser ``fwd/`` relay must never reach an update route on another
    device: fleet updates go through the fleet-key receiver only."""
    for method, path in remote._FWD_ALLOWED:
        assert not path.startswith("/api/update"), path
        assert not path.startswith("/api/fleet"), path
        assert path != "/api/server/restart"


def test_the_member_routes_pass_the_gate_to_their_own_key_check():
    from backend.web.core import auth

    assert ("POST", "/api/fleet/update/apply") in auth._MEMBER_FLEET_ROUTES
    assert ("GET", "/api/fleet/update/state") in auth._MEMBER_FLEET_ROUTES
    # The origin is NOT a member route: it is the owner's, behind the gate.
    assert ("POST", "/api/fleet/update") not in auth._MEMBER_FLEET_ROUTES
