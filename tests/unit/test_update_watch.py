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
    # A release install in ask mode (the suite's own venv is a checkout).
    monkeypatch.setattr(self_update, "install_kind", lambda: "uv-tool")
    monkeypatch.setattr(self_update, "update_mode", lambda: ("ask", False))
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


@pytest.mark.asyncio
async def test_a_failed_lookup_does_not_forget_what_was_announced(monkeypatch, emitted):
    """A GitHub blip used to clear the marker, so the same release was
    announced again on the next good answer."""
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(update_watch, "members_behind", lambda v: [])
    monkeypatch.setattr(self_update, "latest_release", _latest(_RELEASE))
    assert await update_watch.announce() is not None
    monkeypatch.setattr(self_update, "latest_release", _latest(None))
    assert await update_watch.announce() is None
    monkeypatch.setattr(self_update, "latest_release", _latest(_RELEASE))
    assert await update_watch.announce() is None
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_an_install_nobody_can_update_here_is_not_announced(monkeypatch, emitted):
    monkeypatch.setattr(self_update, "latest_release", _latest(_RELEASE))
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "not install.sh")
    monkeypatch.setattr(update_watch, "members_behind", lambda v: [])
    assert await update_watch.announce() is None
    assert emitted == []


@pytest.mark.asyncio
async def test_mode_off_neither_looks_nor_announces(monkeypatch, emitted):
    async def _never(force=False):
        raise AssertionError("looked something up in mode off")

    monkeypatch.setattr(self_update, "update_mode", lambda: ("off", True))
    monkeypatch.setattr(self_update, "latest_release", _never)
    assert await update_watch.announce() is None
    assert emitted == []


@pytest.fixture()
def pending(monkeypatch):
    p = {"key": "", "ref": "", "version": "", "dev": False, "since": 0.0}
    monkeypatch.setattr(update_watch, "_PENDING", p)
    return p


@pytest.mark.asyncio
async def test_auto_mode_is_silent_and_installs_at_the_next_tick(
    monkeypatch, emitted, pending, statedir, restarts
):
    monkeypatch.setattr(self_update, "update_mode", lambda: ("auto", True))
    monkeypatch.setattr(self_update, "latest_release", _latest(_RELEASE))
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(self_update, "managed_by_shell", lambda: False)
    assert await update_watch.announce() is None
    assert emitted == []  # no offer: it just happens
    assert pending["key"] == "v9.9.9"
    started = []

    def _start(ref, auto=False):
        started.append((ref, auto))
        return {"ok": True, "ref": ref}

    monkeypatch.setattr(self_update, "start_update", _start)
    update_watch.tick()
    assert started == [("v9.9.9", True)]


@pytest.mark.asyncio
async def test_auto_mode_leaves_a_desktop_managed_engine_to_the_app(
    monkeypatch, emitted, pending, statedir, restarts
):
    monkeypatch.setattr(self_update, "update_mode", lambda: ("auto", True))
    monkeypatch.setattr(self_update, "latest_release", _latest(_RELEASE))
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(self_update, "managed_by_shell", lambda: True)
    monkeypatch.setattr(
        self_update, "start_update", lambda *a, **k: pytest.fail("installed")
    )
    assert await update_watch.announce() is None
    update_watch.tick()
    assert pending["key"] == "" and emitted == []


def test_a_failed_automatic_start_is_reported_once_and_not_retried(
    monkeypatch, emitted, pending, statedir, restarts
):
    monkeypatch.setattr(self_update, "update_mode", lambda: ("auto", True))
    monkeypatch.setattr(self_update, "managed_by_shell", lambda: False)
    pending.update(key="v9.9.9", ref="v9.9.9", version="9.9.9", since=1.0)
    tries = []

    def _start(ref, auto=False):
        tries.append(ref)
        return {"ok": False, "error": "could not resolve v9.9.9"}

    monkeypatch.setattr(self_update, "start_update", _start)
    update_watch.tick()
    update_watch.tick()
    assert tries == ["v9.9.9"]
    assert [n for n, _ in emitted] == ["update.failed"]
    assert emitted[0][1] == {
        "version": "9.9.9",
        "error": "Could not resolve v9.9.9.",
        "auto": True,
    }
    status = update_watch.auto_status("auto", False, "")
    assert status["active"] is True and status["waiting"] == "retry_later"


def test_a_checkout_update_waits_for_a_pause_in_typing(
    monkeypatch, emitted, pending, statedir, restarts
):
    import time as _time

    monkeypatch.setattr(self_update, "update_mode", lambda: ("auto", True))
    monkeypatch.setattr(self_update, "managed_by_shell", lambda: False)
    pending.update(
        key="c" * 40, ref="main", version="9.9.9", dev=True, since=_time.time()
    )
    typed = {"at": _time.time()}
    monkeypatch.setattr(self_update, "last_human_input", lambda: typed["at"])
    started = []
    monkeypatch.setattr(
        self_update,
        "start_update",
        lambda ref, auto=False: started.append(ref) or {"ok": True},
    )
    update_watch.tick()
    assert started == []
    dev = {"can_update": True, "why_not": ""}
    assert update_watch.auto_status("auto", False, "", dev)["waiting"] == "typing"
    typed["at"] = _time.time() - 61
    update_watch.tick()
    assert started == ["main"]


def test_an_automatic_install_restarts_only_when_nobody_types(
    statedir, restarts, monkeypatch
):
    import time as _time

    now = _time.time()
    typed = {"at": now}
    monkeypatch.setattr(self_update, "last_human_input", lambda: typed["at"])
    self_update.write_state(
        state="done", ref="v9.9.9", commit="a" * 40, code=0, auto=True, finished_at=now
    )
    assert update_watch.tick() is False
    assert self_update.restart_wait() == "typing"
    assert self_update.restart_pending() is True
    typed["at"] = now - 61
    assert update_watch.tick() is True
    assert restarts == ["reexec"]


def test_an_automatic_install_restarts_after_half_an_hour_regardless(
    statedir, restarts, monkeypatch
):
    import time as _time

    monkeypatch.setattr(self_update, "last_human_input", lambda: _time.time())
    self_update.write_state(
        state="done",
        ref="v9.9.9",
        commit="a" * 40,
        code=0,
        auto=True,
        finished_at=_time.time() - 31 * 60,
    )
    assert update_watch.tick() is True


def test_a_clicked_install_restarts_at_once_even_while_typing(
    statedir, restarts, monkeypatch
):
    import time as _time

    monkeypatch.setattr(self_update, "last_human_input", lambda: _time.time())
    self_update.write_state(state="done", ref="v9.9.9", commit="a" * 40, code=0)
    assert update_watch.tick() is True


@pytest.fixture()
def book(statedir, monkeypatch):
    monkeypatch.setattr(update_watch, "_SEEN", {"mark": ""})
    monkeypatch.setattr(update_watch, "_BOOT", {"at": 0.0})
    return statedir


def test_a_failed_update_is_reported_once(book, emitted, restarts):
    import time as _time

    self_update.write_state(
        state="rolled_back",
        ref="v9.9.9",
        version="9.9.9",
        commit="a" * 40,
        started_at=1.0,
        finished_at=_time.time(),
        auto=True,
        error="the new version did not start, so the previous one was put back",
    )
    update_watch.tick()
    update_watch.tick()
    # A second server process (or a restart) doesn't say it again either.
    update_watch._SEEN["mark"] = ""
    update_watch.tick()
    assert [n for n, _ in emitted] == ["update.failed"]
    data = emitted[0][1]
    assert data["auto"] is True and data["version"] == "9.9.9"
    assert data["error"].startswith("The new version did not start")
    assert update_watch._retry_blocked("v9.9.9") is True


def test_an_interrupted_update_says_so_plainly(book, emitted, restarts):
    import time as _time

    self_update.write_state(
        state="failed",
        ref="v9.9.9",
        version="9.9.9",
        error="interrupted",
        finished_at=_time.time(),
    )
    update_watch.tick()
    assert emitted[0][1]["error"] == "The update stopped before it finished."
    assert emitted[0][1]["auto"] is False


def test_done_is_said_once_from_the_restarted_process(
    book, emitted, restarts, monkeypatch
):
    import time as _time

    self_update.write_state(
        state="done",
        ref="v9.9.9",
        version="9.9.9",
        from_version="9.9.8",
        commit="a" * 40,
        started_at=1.0,
        finished_at=_time.time(),
        restarted=True,
        auto=True,
    )
    # Still the old process (it doesn't run the new build): not yet.
    update_watch.tick()
    assert emitted == []
    # The restarted one runs it: said once, even after the installer's
    # health check rewrites the state file.
    monkeypatch.setattr(self_update, "installed_commit", lambda: "a" * 40)
    update_watch.tick()
    st = self_update.read_state()
    self_update.write_state(**{**st, "healthy": True})
    update_watch._SEEN["mark"] = ""
    update_watch.tick()
    assert [n for n, _ in emitted] == ["update.done"]
    assert emitted[0][1] == {
        "version": "9.9.9",
        "from_version": "9.9.8",
        "auto": True,
        "notes_url": "https://github.com/%s/releases/tag/v9.9.9"
        % self_update.UPDATE_REPO,
    }


def test_done_waits_for_the_browsers_to_reconnect(book, emitted, restarts, monkeypatch):
    import time as _time

    monkeypatch.setattr(update_watch, "_BOOT", {"at": _time.time()})
    monkeypatch.setattr(self_update, "installed_commit", lambda: "a" * 40)
    self_update.write_state(
        state="done",
        ref="v9.9.9",
        version="9.9.9",
        commit="a" * 40,
        finished_at=_time.time(),
        restarted=True,
    )
    update_watch.tick()
    assert emitted == []


def test_an_old_outcome_is_history_not_news(book, emitted, restarts):
    self_update.write_state(
        state="failed", ref="v1.0.0", version="1.0.0", finished_at=1.0
    )
    update_watch.tick()
    assert emitted == []


# --------------------------------------------------------------------------- #
# Development checkouts
# --------------------------------------------------------------------------- #
def _dev(**over):
    st = {
        "checkout": "/src/mf",
        "branch": "main",
        "default_branch": "main",
        "dirty": False,
        "behind": 2,
        "ahead": 0,
        "can_update": True,
        "why_not": "",
        "remote": "origin",
        "upstream": "origin/main",
        "head": "b" * 40,
        "target": "c" * 40,
        "target_version": "9.9.9",
    }
    st.update(over)
    return st


@pytest.mark.asyncio
async def test_a_checkout_hears_about_its_own_branch_never_releases(
    monkeypatch, emitted, pending
):
    from backend.web.core import dev_update

    async def _never(force=False):
        raise AssertionError("a checkout asked GitHub")

    monkeypatch.setattr(self_update, "install_kind", lambda: "editable")
    monkeypatch.setattr(self_update, "latest_release", _never)
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(dev_update, "status", lambda *a, **k: _dev())
    data = await update_watch.announce()
    assert data["dev"] is True and data["here"] is True and data["tag"] == ""
    assert "development checkout" in data["detail"]
    assert await update_watch.announce() is None  # once per target


@pytest.mark.asyncio
async def test_a_checkout_that_cannot_update_is_quiet(monkeypatch, emitted, pending):
    from backend.web.core import dev_update

    monkeypatch.setattr(self_update, "install_kind", lambda: "editable")
    monkeypatch.setattr(
        dev_update,
        "status",
        lambda *a, **k: _dev(branch="foo", can_update=False, why_not="on foo"),
    )
    assert await update_watch.announce() is None
    monkeypatch.setattr(self_update, "update_mode", lambda: ("auto", True))
    assert await update_watch.announce() is None
    assert emitted == [] and pending["key"] == ""


@pytest.mark.asyncio
async def test_a_checkout_in_auto_mode_queues_its_target(monkeypatch, emitted, pending):
    from backend.web.core import dev_update

    monkeypatch.setattr(self_update, "install_kind", lambda: "editable")
    monkeypatch.setattr(self_update, "update_mode", lambda: ("auto", True))
    monkeypatch.setattr(dev_update, "status", lambda *a, **k: _dev())
    assert await update_watch.announce() is None
    assert pending["key"] == "c" * 40 and pending["dev"] is True
    assert emitted == []


def test_the_status_line_per_mode(monkeypatch, pending):
    monkeypatch.setattr(self_update, "restart_wait", lambda st=None: "")
    off = update_watch.auto_status("off", False, "")
    assert off == {"active": False, "why": update_watch._WHY["off"], "waiting": ""}
    assert update_watch.auto_status("ask", False, "")["active"] is False
    on = update_watch.auto_status("auto", False, "")
    assert on["active"] is True and "pause" in on["why"]
    shell = update_watch.auto_status("auto", True, "")
    assert shell["active"] is False and "desktop app" in shell["why"]
    dev = _dev(
        can_update=False, why_not="This is a development checkout on branch foo."
    )
    blocked = update_watch.auto_status("auto", False, "", dev)
    assert blocked["active"] is False and blocked["why"] == dev["why_not"]
