"""``mindflock slot`` (:mod:`backend.slot`) — the machine-wide heavy-job gate.

Pins the semaphore (N holders, a waiter proceeding when a holder exits, a
SIGKILLed holder releasing by construction), the exit-status passthrough
(codes and signal deaths), signal forwarding, the bypass and nested-holder
paths, the capped memory hold, the slot count's precedence, the argv split
that keeps a wrapped command's own flags intact, and the status route.

Every lock lives under ``$MINDFLOCK_RUN_DIR`` (tests/conftest.py points it at
a per-test tmp dir), and the memory guard is off unless a test feeds it.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from backend import slot

REPO = Path(__file__).resolve().parents[2]


def _env(**extra):
    env = dict(os.environ)
    env.pop("MINDFLOCK_SLOT_HELD", None)
    env.pop("MINDFLOCK_SLOT", None)
    env.pop("MINDFLOCK_HEAVY_SLOTS", None)
    # Never let a record point at the owner's real tmux server.
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _spawn(args, *, env=None, **kw):
    """``python -m backend.slot ARGS`` with no terminal (its own session, so a
    SIGINT we send is the only one the child could get)."""
    return subprocess.Popen(
        [sys.executable, "-m", "backend.slot", *args],
        cwd=str(REPO),
        env=env or _env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **kw,
    )


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for %s" % path)
        time.sleep(0.02)


def _holder_script(marker: Path, seconds: float = 30.0) -> str:
    """A child that writes its pid to ``marker`` and sleeps."""
    return textwrap.dedent("""
        import os, sys, time
        open(%r, "w").write(str(os.getpid()))
        time.sleep(%r)
        """ % (str(marker), seconds))


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    d = tmp_path / "run"
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(d))
    monkeypatch.delenv("MINDFLOCK_HEAVY_SLOTS", raising=False)
    monkeypatch.delenv("MINDFLOCK_SLOT_HELD", raising=False)
    monkeypatch.delenv("MINDFLOCK_SLOT", raising=False)
    return d


# --------------------------------------------------------------------------- #
# The semaphore
# --------------------------------------------------------------------------- #
def test_n_holders_then_none_and_a_release_frees_one(run_dir):
    a = slot.try_acquire("heavy", 2, {"pid": os.getpid()})
    b = slot.try_acquire("heavy", 2, {"pid": os.getpid()})
    assert a is not None and b is not None and {a.index, b.index} == {0, 1}
    assert slot.try_acquire("heavy", 2) is None
    # Another pool is its own semaphore.
    other = slot.try_acquire("docs", 2)
    assert other is not None
    a.release()
    c = slot.try_acquire("heavy", 2)
    assert c is not None and c.index == a.index
    for s in (b, c, other):
        s.release()
    # The record goes with the slot.
    assert not (run_dir / "slots" / "heavy" / "slot-0.json").exists()


def test_the_slot_dir_is_the_run_dir(run_dir):
    assert slot.slots_root() == str(run_dir / "slots")
    with pytest.raises(ValueError):
        slot.pool_dir("../escape")


def test_acquire_times_out_and_cleans_its_queue_entry(run_dir):
    held = slot.try_acquire("heavy", 1)
    told = []
    got = slot.acquire(
        "heavy",
        1,
        timeout=0.05,
        poll=0.01,
        on_wait=lambda holders, ahead: told.append(ahead),
    )
    assert got is None and told == [0]
    names = os.listdir(run_dir / "slots" / "heavy")
    assert not [n for n in names if n.startswith("wait-")]
    held.release()


def test_a_dead_waiter_is_reaped_and_does_not_block_the_queue(run_dir):
    pool = run_dir / "slots" / "heavy"
    pool.mkdir(parents=True)
    # A waiter file nobody holds a lock on: its process is gone.
    (pool / "wait-00000000000000000001-99999.json").write_text("{}")
    got = slot.acquire("heavy", 1, timeout=1.0, poll=0.01)
    assert got is not None
    assert not (pool / "wait-00000000000000000001-99999.json").exists()
    got.release()


def test_a_waiter_proceeds_when_the_holder_exits(run_dir, tmp_path):
    marker = tmp_path / "a-started"
    a = _spawn(
        ["--no-wait-memory", "--", sys.executable, "-c", _holder_script(marker, 1.0)]
    )
    _wait_for(marker)
    t0 = time.monotonic()
    b = _spawn(["--no-wait-memory", "--", sys.executable, "-c", "print('B ran')"])
    out_b, err_b = b.communicate(timeout=30)
    a.communicate(timeout=30)
    assert b.returncode == 0 and "B ran" in out_b
    # B had to wait for A, said so ONCE, and named A's command and pid.
    assert time.monotonic() - t0 >= 0.3
    lines = [l for l in err_b.splitlines() if "waiting for a heavy-job slot" in l]
    assert len(lines) == 1, err_b
    assert "pid %d" % a.pid in lines[0] and "MINDFLOCK_SLOT=0" in lines[0]
    # Outside tmux and any session, the holder is named by its folder.
    assert "held by a job in %s, pid" % REPO.name in lines[0]
    assert a.returncode == 0


def test_a_killed_holder_releases_its_slot(run_dir, tmp_path):
    marker = tmp_path / "started"
    a = _spawn(["--no-wait-memory", "--", sys.executable, "-c", _holder_script(marker)])
    _wait_for(marker)
    child = int(marker.read_text())
    assert slot.try_acquire("heavy", 1) is None
    a.kill()  # SIGKILL: no handler, no cleanup — only the kernel's flock release
    a.wait(timeout=10)
    try:
        got = slot.try_acquire("heavy", 1)
        assert got is not None
        got.release()
    finally:
        os.kill(child, signal.SIGKILL)


# --------------------------------------------------------------------------- #
# Running the command
# --------------------------------------------------------------------------- #
def test_the_exit_code_passes_through(run_dir):
    p = _spawn(["--", sys.executable, "-c", "import sys; sys.exit(7)"])
    p.communicate(timeout=30)
    assert p.returncode == 7


def test_a_signal_death_passes_through_as_the_same_signal(run_dir):
    p = _spawn(
        [
            "--",
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ]
    )
    p.communicate(timeout=30)
    assert p.returncode == -signal.SIGTERM


def test_a_missing_command_is_127(run_dir):
    p = _spawn(["--", "definitely-not-a-command-xyz"])
    _, err = p.communicate(timeout=30)
    assert p.returncode == 127 and "command not found" in err


def test_stdin_and_stdout_belong_to_the_command(run_dir):
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.slot",
            "--",
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(sys.stdin.read().upper())",
        ],
        cwd=str(REPO),
        env=_env(),
        input="piped through",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert p.returncode == 0 and p.stdout == "PIPED THROUGH"


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signals_are_forwarded_to_the_command_and_the_slot_freed(
    run_dir, tmp_path, sig
):
    marker = tmp_path / "ready"
    got = tmp_path / "got"
    script = textwrap.dedent("""
        import signal, sys, time
        def on(signum, frame):
            open(%r, "w").write(str(signum))
            sys.exit(42)
        signal.signal(signal.SIGTERM, on)
        signal.signal(signal.SIGINT, on)
        open(%r, "w").write("x")
        time.sleep(30)
        """ % (str(got), str(marker)))
    p = _spawn(["--no-wait-memory", "--", sys.executable, "-c", script])
    _wait_for(marker)
    p.send_signal(sig)
    p.communicate(timeout=30)
    assert p.returncode == 42
    assert got.read_text() == str(int(sig))
    s = slot.try_acquire("heavy", 1)
    assert s is not None
    s.release()


def test_the_command_knows_its_pool_is_held_and_a_nested_slot_runs_through(run_dir):
    # One slot: a nested `mindflock slot` would wait on its own parent forever.
    inner = "import subprocess, sys; sys.exit(subprocess.call([sys.executable, '-m', 'backend.slot', '--', sys.executable, '-c', 'import os; print(os.environ[\"MINDFLOCK_SLOT_HELD\"])']))"
    p = _spawn(["--max", "1", "--", sys.executable, "-c", inner])
    out, err = p.communicate(timeout=30)
    assert p.returncode == 0, err
    assert out.strip() == "heavy"
    assert "waiting" not in err


def test_mindflock_slot_0_bypasses_the_gate(run_dir):
    held = slot.try_acquire("heavy", 1)
    try:
        p = _spawn(
            ["--", sys.executable, "-c", "import sys; print('bypassed'); sys.exit(3)"],
            env=_env(MINDFLOCK_SLOT="0"),
        )
        out, err = p.communicate(timeout=10)
        assert p.returncode == 3 and "bypassed" in out and "waiting" not in err
    finally:
        held.release()


def test_the_cli_dispatches_slot_before_its_own_parser(run_dir, monkeypatch):
    from backend import cli

    seen = []
    monkeypatch.setattr(slot, "run", lambda cmd, **kw: seen.append((cmd, kw)) or 5)
    # `--max` after the command belongs to the command, not to us.
    assert (
        cli.main(["slot", "--pool", "docs", "--", "pytest", "-n", "auto", "--max", "9"])
        == 5
    )
    cmd, kw = seen[0]
    assert cmd == ["pytest", "-n", "auto", "--max", "9"]
    assert kw["pool"] == "docs" and kw["n"] is None


def test_split_argv():
    assert slot.split_argv(["--max", "2", "--", "a", "-b"]) == (
        ["--max", "2"],
        ["a", "-b"],
    )
    assert slot.split_argv(["--pool", "x", "pytest", "--", "-k"]) == (
        ["--pool", "x"],
        ["pytest", "--", "-k"],
    )
    assert slot.split_argv(["--no-wait-memory", "env", "A=1", "uv"]) == (
        ["--no-wait-memory"],
        ["env", "A=1", "uv"],
    )
    assert slot.split_argv([]) == ([], [])


def test_no_command_is_a_usage_error(run_dir):
    err = io.StringIO()
    assert slot.run([], err=err) == 2


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #
def test_the_memory_hold_is_capped_then_runs_anyway(monkeypatch):
    from backend import memory_pressure

    monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: True)
    monkeypatch.setattr(
        memory_pressure, "describe", lambda *a: "memory is low: 1.0 GB free"
    )
    clock = [0.0]
    monkeypatch.setattr(slot.time, "monotonic", lambda: clock[0])

    def sleep(s):
        clock[0] += s

    err = io.StringIO()
    assert slot.wait_for_memory(cap=600, poll=5, err=err, sleep=sleep) is False
    assert clock[0] >= 600
    text = err.getvalue()
    assert (
        "memory is low: 1.0 GB free — holding until it recovers (at most 10 min)"
        in text
    )
    assert "running anyway after 10 min held for memory" in text


def test_the_memory_hold_ends_when_memory_recovers(monkeypatch):
    from backend import memory_pressure

    readings = iter([True, True, False])
    monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: next(readings, False))
    monkeypatch.setattr(memory_pressure, "describe", lambda *a: "memory is low")
    slept = []
    assert (
        slot.wait_for_memory(cap=600, poll=5, err=io.StringIO(), sleep=slept.append)
        is True
    )
    assert slept == [5]


def test_ok_memory_never_waits_or_prints(monkeypatch):
    from backend import memory_pressure

    monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: False)
    err = io.StringIO()
    assert slot.wait_for_memory(err=err, sleep=lambda s: pytest.fail("slept")) is True
    assert err.getvalue() == ""


def test_run_waits_for_memory_while_holding_the_slot(run_dir, monkeypatch):
    calls = []

    def fake_wait(**kw):
        # The slot is already ours while memory is awaited.
        calls.append(slot.try_acquire("heavy", 1))
        return True

    monkeypatch.setattr(slot, "wait_for_memory", fake_wait)
    rc = slot.run([sys.executable, "-c", "pass"], n=1, err=io.StringIO())
    assert rc == 0 and calls == [None]
    skipped = []
    monkeypatch.setattr(slot, "wait_for_memory", lambda **kw: skipped.append(1))
    assert (
        slot.run(
            [sys.executable, "-c", "pass"], n=1, wait_memory=False, err=io.StringIO()
        )
        == 0
    )
    assert skipped == []


# --------------------------------------------------------------------------- #
# How many slots
# --------------------------------------------------------------------------- #
def test_default_slots_by_ram():
    GiB = 1024**3
    assert slot.default_slots(24 * GiB) == 1
    assert slot.default_slots(int(31.2 * GiB)) == 2  # a nominal 32 GB box
    assert slot.default_slots(64 * GiB) == 2
    assert slot.default_slots(0) == 1  # unknown


def test_configured_slots_precedence(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(settings))
    monkeypatch.delenv("MINDFLOCK_HEAVY_SLOTS", raising=False)
    monkeypatch.setattr(slot, "default_slots", lambda total_bytes=None: 1)
    assert slot.configured_slots() == {"slots": 1, "source": "default"}
    settings.write_text(json.dumps({"general": {"heavy_slots": 3}}))
    assert slot.configured_slots() == {"slots": 3, "source": "setting"}
    settings.write_text(json.dumps({"general": {"heavy_slots": 0}}))
    assert slot.configured_slots()["source"] == "default"
    settings.write_text("{not json")
    assert slot.configured_slots()["source"] == "default"
    monkeypatch.setenv("MINDFLOCK_HEAVY_SLOTS", "4")
    assert slot.configured_slots() == {"slots": 4, "source": "env"}


def test_the_setting_round_trips_through_the_store(tmp_path, monkeypatch):
    from backend.config import settings as store

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "s.json"))
    store.invalidate()
    store.update_settings(general={"heavy_slots": 2})
    assert store.load_settings().general.heavy_slots == 2
    assert json.loads((tmp_path / "s.json").read_text())["general"]["heavy_slots"] == 2
    assert store.GeneralSettings.from_dict({"heavy_slots": -1}).heavy_slots is None
    store.invalidate()


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def test_status_maps_holders_and_waiters_to_sessions(run_dir, tmp_path):
    wt = tmp_path / "worktrees" / "fix-auth"
    (wt / "sub").mkdir(parents=True)
    held = slot.try_acquire(
        "heavy",
        1,
        {
            "pid": os.getpid(),
            "cmd": "uv run pytest",
            "cwd": str(wt / "sub"),
            "since": 1.0,
        },
    )
    try:
        st = slot.status(
            [
                {
                    "title": "fix-auth",
                    "tmux_name": "mindflock_fix-auth",
                    "folder": str(wt),
                },
                {
                    "title": "other",
                    "tmux_name": "mindflock_other",
                    "folder": str(tmp_path / "x"),
                },
            ],
            resolve_panes=False,
        )
        assert st["running"] == 1 and st["waiting"] == 0
        (pool,) = st["pools"]
        assert pool["name"] == "heavy"
        assert pool["holders"] == [
            {
                "pid": os.getpid(),
                "cmd": "uv run pytest",
                "cwd": str(wt / "sub"),
                "since": 1.0,
                "slot": 0,
                "session": "fix-auth",
            }
        ]
    finally:
        held.release()
    assert slot.status()["pools"] == []


def test_session_for_prefers_the_baked_title_then_the_pane():
    sessions = [
        {"title": "a b", "tmux_name": "mindflock_ab", "folder": "/w/a"},
        {"title": "c", "tmux_name": "mindflock_c", "folder": "/w/c"},
    ]
    panes = {("/s", "%1"): "mindflock_ab_sh"}
    assert slot._session_for({"session": "zz"}, panes, sessions) == "zz"
    assert (
        slot._session_for(
            {"tmux_socket": "/s", "tmux_pane": "%1", "cwd": "/w/c"}, panes, sessions
        )
        == "a b"
    )
    assert slot._session_for({"cwd": "/w/c/deep"}, panes, sessions) == "c"
    assert slot._session_for({"cwd": "/elsewhere"}, panes, sessions) == ""


def test_the_route_serves_slots(run_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    # The engine is the live state: an empty flock, not the owner's sessions.
    monkeypatch.setattr(server.ENGINE, "instances", {})
    held = slot.try_acquire(
        "heavy", 1, {"pid": os.getpid(), "cmd": "make", "cwd": "/nowhere"}
    )
    try:
        body = TestClient(server.app).get("/api/system/slots").json()
    finally:
        held.release()
    assert body["running"] == 1 and body["waiting"] == 0
    assert body["slots"] >= 1 and body["source"] in ("env", "setting", "default")
    assert body["pools"][0]["holders"][0]["cmd"] == "make"
    assert body["pools"][0]["holders"][0]["session"] == ""
