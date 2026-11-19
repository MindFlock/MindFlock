"""Pruning the activity-marker directory (backend/web/core/marker_sweep.py):
only files of a session that is not live AND older than the age line go."""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from backend.web.core import marker_sweep as ms

DAY = 24 * 3600.0


def _touch(d, name, age_s, now):
    p = d / name
    p.write_text("{}")
    os.utime(p, (now - age_s, now - age_s))
    return p


@pytest.fixture
def now():
    return time.time()


def test_an_old_dead_sessions_files_go(tmp_path, now):
    a = _touch(tmp_path, "mindflock_gone.json", 30 * DAY, now)
    b = _touch(tmp_path, "mindflock_gone.reply.json", 30 * DAY, now)
    c = _touch(tmp_path, "mindflock_gone.reply.json.123.tmp", 30 * DAY, now)
    assert ms.sweep(tmp_path, set(), now=now) == 3
    assert not a.exists() and not b.exists() and not c.exists()


def test_a_live_sessions_files_stay_however_old(tmp_path, now):
    """A sleeping session's last reply is read with no age limit (question
    detection, what it last said): live means kept."""
    a = _touch(tmp_path, "mindflock_live.json", 90 * DAY, now)
    b = _touch(tmp_path, "mindflock_live.reply.json", 90 * DAY, now)
    assert ms.sweep(tmp_path, {"mindflock_live"}, now=now) == 0
    assert a.exists() and b.exists()


def test_a_recent_dead_sessions_files_stay(tmp_path, now):
    a = _touch(tmp_path, "mindflock_ended.json", 2 * DAY, now)
    b = _touch(tmp_path, "mindflock_ended.reply.json", 6 * DAY, now)
    assert ms.sweep(tmp_path, set(), now=now) == 0
    assert a.exists() and b.exists()


def test_files_it_does_not_own_stay(tmp_path, now):
    keep = [
        _touch(tmp_path, "notes.txt", 90 * DAY, now),
        _touch(tmp_path, "x.thread", 90 * DAY, now),
    ]
    (tmp_path / "old.json.d").mkdir()
    assert ms.sweep(tmp_path, set(), now=now) == 0
    assert all(p.exists() for p in keep)


def test_a_failure_to_delete_is_swallowed(tmp_path, now):
    _touch(tmp_path, "mindflock_a.json", 30 * DAY, now)
    b = _touch(tmp_path, "mindflock_b.json", 30 * DAY, now)

    def flaky(path):
        if path.endswith("mindflock_a.json"):
            raise PermissionError(path)
        os.unlink(path)

    assert ms.sweep(tmp_path, set(), now=now, unlink=flaky) == 1
    assert not b.exists()


def test_a_missing_directory_removes_nothing(tmp_path, now):
    assert ms.sweep(tmp_path / "nope", set(), now=now) == 0


def test_a_pass_is_bounded(tmp_path, now):
    for i in range(12):
        _touch(tmp_path, "mindflock_s%d.json" % i, 30 * DAY, now)
    assert ms.sweep(tmp_path, set(), now=now, limit=5) == 5
    assert len(list(tmp_path.iterdir())) == 7


def test_live_names_cover_state_sessions_and_every_tmux_session():
    from backend.providers import activity_markers as am
    from backend.session import tmux

    names = ms.live_names(["fix auth"], ["cursor-window", "antislop"])
    assert am.safe_session_name(tmux.to_mindflock_tmux_name("fix auth")) in names
    assert {"cursor-window", "antislop"} <= names


def _srv(titles):
    return SimpleNamespace(
        ENGINE=SimpleNamespace(instances={t: object() for t in titles}),
        log=SimpleNamespace(InfoLog=None, ErrorLog=None),
    )


def test_no_pass_when_tmux_cannot_be_asked(tmp_path, monkeypatch, now):
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path))
    p = _touch(tmp_path, "mindflock_x.json", 30 * DAY, now)
    monkeypatch.setattr(ms, "_tmux_sessions", lambda: None)
    assert ms.pass_once(_srv([])) == 0
    assert p.exists()


def test_a_pass_keeps_state_and_tmux_sessions(tmp_path, monkeypatch, now):
    from backend.providers import activity_markers as am
    from backend.session import tmux

    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path))
    asleep = am.safe_session_name(tmux.to_mindflock_tmux_name("asleep"))
    kept = [
        _touch(tmp_path, asleep + ".reply.json", 30 * DAY, now),
        _touch(tmp_path, "pipeline-run.json", 30 * DAY, now),
    ]
    gone = _touch(tmp_path, "mindflock_gone.json", 30 * DAY, now)
    monkeypatch.setattr(ms, "_tmux_sessions", lambda: {"pipeline-run"})
    assert ms.pass_once(_srv(["asleep"])) == 1
    assert all(p.exists() for p in kept) and not gone.exists()


def test_tmux_without_a_server_reads_as_no_sessions(monkeypatch):
    from backend.session import mux

    monkeypatch.setattr(
        mux,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="", stderr="no server running on /tmp/tmux-1/default"
        ),
    )
    assert ms._tmux_sessions() == set()
    monkeypatch.setattr(
        mux,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="odd"),
    )
    assert ms._tmux_sessions() is None


def test_the_loop_runs_on_the_tick_pool_and_never_dies(monkeypatch):
    calls = []

    async def in_pool(fn, *a):
        calls.append(fn)
        raise RuntimeError("boom")

    class _Stop(Exception):
        pass

    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) > 2:
            raise _Stop

    monkeypatch.setattr(ms.asyncio, "sleep", sleep)
    with pytest.raises(_Stop):
        asyncio.run(ms.run_loop(SimpleNamespace(_in_bg_pool=in_pool), first_delay=1))
    assert calls == [ms.pass_once, ms.pass_once]
    assert sleeps == [1, ms.INTERVAL_S, ms.INTERVAL_S]


def test_off_under_pytest_and_by_env(monkeypatch):
    monkeypatch.setattr(ms, "ENABLED", None)
    assert ms.enabled() is False
    from backend.web.core import restart

    monkeypatch.setattr(restart, "_under_pytest", lambda: False)
    assert ms.enabled() is True
    monkeypatch.setenv("MINDFLOCK_MARKER_SWEEP", "0")
    assert ms.enabled() is False
