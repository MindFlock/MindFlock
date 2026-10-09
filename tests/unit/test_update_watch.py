"""The server applies its own finished update (core.update_watch).

The restart onto a new engine used to happen only when a browser polled
``/api/update/state`` after the install finished; with the tab closed (or the
update started from another device) the server kept serving old Python from a
replaced venv. The watcher closes that, and these pin its three promises:
restart exactly once when an install is done, never restart a process that
already runs the installed build, and hold off while Setup's install terminal
is running. Plus the ``update.available`` notice it emits.
"""

from __future__ import annotations

import pytest

from backend.web.core import events
from backend.web.core import self_update
from backend.web.core import update_watch


@pytest.fixture()
def statedir(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "_state_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture()
def restarts(monkeypatch):
    calls = []

    def _reexec(**kw):
        # An update keeps the serve mode (a tailscale rig stays on the tailnet).
        assert kw == {"keep_mode": True}
        calls.append("reexec")

    monkeypatch.setattr(update_watch._restart, "reexec_soon", _reexec)
    monkeypatch.setattr(update_watch._restart, "reset_tailscale_attempts", lambda: None)
    monkeypatch.setattr(self_update, "_install_terminal_busy", lambda: False)
    monkeypatch.setattr(self_update, "_RESTARTING", {"key": ""})
    monkeypatch.setattr(self_update, "installed_commit", lambda: "0" * 40)
    return calls


def test_a_finished_install_restarts_the_server_with_nobody_watching(
    statedir, restarts
):
    self_update.write_state(state="done", ref="v9.9.9", commit="a" * 40, code=0)
    assert update_watch.tick() is True
    assert restarts == ["reexec"]
    # Once: the flag lives in the state file, so the next tick (or a polling
    # screen) doesn't restart the fresh process again.
    assert update_watch.tick() is False
    assert restarts == ["reexec"]


def test_nothing_happens_while_the_install_runs_or_after_it_failed(statedir, restarts):
    import os
    import time

    self_update.write_state(state="started", started_at=time.time(), pid=os.getpid())
    assert update_watch.tick() is False
    self_update.write_state(state="failed", ref="v9.9.9", code=1)
    assert update_watch.tick() is False
    self_update.write_state(state="rolled_back", ref="v9.9.9", code=0)
    assert update_watch.tick() is False
    assert restarts == []


def test_a_process_already_on_the_installed_build_is_never_restarted(
    statedir, restarts, monkeypatch
):
    monkeypatch.setattr(self_update, "installed_commit", lambda: "a" * 40)
    self_update.write_state(state="done", ref="v9.9.9", commit="a" * 40, code=0)
    assert update_watch.tick() is False
    assert restarts == []
    # …and it is marked, so it reads as applied from now on.
    assert self_update.read_state().get("restarted") is True


def test_the_restart_waits_for_setups_install_terminal(statedir, restarts, monkeypatch):
    busy = {"on": True}
    monkeypatch.setattr(self_update, "_install_terminal_busy", lambda: busy["on"])
    self_update.write_state(state="done", ref="v9.9.9", commit="a" * 40, code=0)
    assert update_watch.tick() is False
    assert restarts == []
    assert self_update.restart_pending() is True  # the screen says so meanwhile
    busy["on"] = False
    assert update_watch.tick() is True
    assert restarts == ["reexec"]


def test_a_dead_installer_is_settled_as_interrupted_by_the_watcher(statedir, restarts):
    import subprocess
    import time

    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    proc.wait()
    self_update.write_state(state="started", started_at=time.time(), pid=proc.pid)
    update_watch.tick()
    st = self_update.read_state()
    assert st["state"] == "failed" and st["error"] == "interrupted"


def test_tick_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(self_update, "read_state", boom)
    assert update_watch.tick() is False


# --------------------------------------------------------------------------- #
# update.available
# --------------------------------------------------------------------------- #
_RELEASE = {"tag": "v9.9.9", "version": "9.9.9"}


@pytest.fixture()
def emitted(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        events.BUS, "emit", lambda name, **kw: seen.append((name, kw.get("data")))
    )
    monkeypatch.setattr(update_watch, "_LAST", {"sig": "", "loaded": False})
    monkeypatch.setattr(update_watch, "_last_path", lambda: tmp_path / "ann.json")
    return seen


def _latest(release):
    async def _fn(force=False):
        return release

    return _fn


@pytest.mark.asyncio
async def test_a_newer_release_is_announced_once(monkeypatch, emitted):
    monkeypatch.setattr(self_update, "latest_release", _latest(_RELEASE))
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(update_watch, "members_behind", lambda v: [])
    data = await update_watch.announce()
    assert data["here"] is True and data["count"] == 1
    assert "v9.9.9 is out" in data["detail"]
    assert await update_watch.announce() is None  # same answer: no second event
    assert [name for name, _ in emitted] == ["update.available"]


@pytest.mark.asyncio
async def test_a_restart_does_not_announce_the_same_answer_again(monkeypatch, emitted):
    """Every update ends in a restart; the marker is on disk, not in memory."""
    monkeypatch.setattr(self_update, "latest_release", _latest(_RELEASE))
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(update_watch, "members_behind", lambda v: [])
    assert await update_watch.announce() is not None
    monkeypatch.setattr(update_watch, "_LAST", {"sig": "", "loaded": False})
    assert await update_watch.announce() is None
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_the_notice_counts_the_other_devices_behind(monkeypatch, emitted):
    monkeypatch.setattr(self_update, "latest_release", _latest(_RELEASE))
    monkeypatch.setattr(self_update, "installed_version", lambda: "9.9.9")
    behind = [
        {"key": "mini", "host": "Mac mini", "version": "0.6.1"},
        {"key": "rig", "host": "ML-Rig", "version": "0.7.4"},
    ]
    monkeypatch.setattr(update_watch, "members_behind", lambda v: behind)
    data = await update_watch.announce()
    assert data["here"] is False and data["count"] == 2
    assert data["detail"].endswith("2 of your devices are behind")
    # One of them catches up: a different answer, so a new event.
    monkeypatch.setattr(update_watch, "members_behind", lambda v: behind[:1])
    assert (await update_watch.announce())["count"] == 1
    assert len(emitted) == 2


@pytest.mark.asyncio
async def test_nothing_is_announced_when_everyone_is_current_or_github_is_down(
    monkeypatch, emitted
):
    monkeypatch.setattr(self_update, "installed_version", lambda: "9.9.9")
    monkeypatch.setattr(update_watch, "members_behind", lambda v: [])
    monkeypatch.setattr(self_update, "latest_release", _latest(_RELEASE))
    assert await update_watch.announce() is None
    monkeypatch.setattr(self_update, "latest_release", _latest(None))
    assert await update_watch.announce() is None
    assert emitted == []
