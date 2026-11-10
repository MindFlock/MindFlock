"""``mux.spawn_pty``: a PTY client started by ``posix_spawn``, not ``fork()``.

The real thing, against a throwaway tmux server on its OWN socket
(``tmux -L <unique>``, ``TMUX`` unset): never the default socket, never a
``mindflock_*`` session, and only that private server is ever killed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from backend.session import mux

needs_posix_spawn = pytest.mark.skipif(
    not mux._POSIX_SPAWN_PTY, reason="Linux posix_spawn PTY"
)
needs_tmux = pytest.mark.skipif(
    not mux._POSIX_SPAWN_PTY or shutil.which("tmux") is None,
    reason="Linux posix_spawn PTY + tmux",
)


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["TERM"] = "xterm-256color"
    return env


def _until(cond, timeout=5.0):
    end = time.time() + timeout
    while True:
        got = cond()
        if got:
            return got
        assert time.time() < end, "timed out"
        time.sleep(0.05)


def _drain(proc) -> None:
    """Read what the client drew so its output never fills the PTY."""
    import select

    while select.select([proc.fd], [], [], 0)[0]:
        try:
            if not os.read(proc.fd, 65536):
                return
        except OSError:
            return


@pytest.fixture
def tmux_server():
    sock = "mf-test-attach-" + uuid.uuid4().hex[:10]
    base = ["tmux", "-L", sock]
    env = _env()
    subprocess.run(
        base
        + [
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "scratch",
            "-x",
            "80",
            "-y",
            "24",
            "sleep 300",
        ],
        check=True,
        env=env,
    )
    try:
        yield base, env
    finally:
        subprocess.run(base + ["kill-server"], env=env, capture_output=True)


def _clients(base, env) -> list:
    out = subprocess.run(
        base + ["list-clients", "-F", "#{client_width}x#{client_height}"],
        env=env,
        capture_output=True,
        text=True,
    ).stdout
    return out.split()


@needs_tmux
def test_a_real_tmux_attach_sizes_resizes_and_detaches(tmux_server):
    base, env = tmux_server
    proc = mux.spawn_pty(
        base + ["attach-session", "-t", "scratch"], dimensions=(30, 100), env=env
    )
    try:
        assert proc.isalive()
        assert proc.getwinsize() == (30, 100)
        # Attached at the size it was spawned with.
        assert _until(lambda: (_drain(proc), _clients(base, env))[1]) == ["100x30"]
        # A resize reaches the client: only true when the PTY is its
        # controlling tty with its group in the foreground (SIGWINCH).
        proc.setwinsize(40, 120)
        _until(lambda: (_drain(proc), _clients(base, env) == ["120x40"])[1])
    finally:
        proc.terminate(force=True)
    _until(lambda: not proc.isalive())
    _until(lambda: _clients(base, env) == [])
    # The session itself keeps running (only the client went).
    assert (
        subprocess.run(base + ["has-session", "-t", "scratch"], env=env).returncode == 0
    )


@needs_tmux
def test_closing_the_master_hangs_up_the_client(tmux_server):
    base, env = tmux_server
    proc = mux.spawn_pty(base + ["attach-session", "-t", "scratch"], env=env)
    _until(lambda: (_drain(proc), _clients(base, env))[1])
    proc.close(force=False)  # no signals from us: the hangup alone ends it
    _until(lambda: _clients(base, env) == [])


def _read_all(proc, timeout=10.0) -> bytes:
    """Everything the child prints until it exits (EIO once the slave closes)."""
    out = b""
    end = time.time() + timeout
    while time.time() < end:
        try:
            chunk = os.read(proc.fd, 4096)
        except OSError:  # EIO: the child exited and the slave closed
            break
        if not chunk:
            break
        out += chunk
    return out


@needs_posix_spawn
def test_the_child_gets_a_controlling_tty_and_the_env():
    code = (
        "import os,sys;"
        "fg = os.tcgetpgrp(0) == os.getpgrp() == os.getpid();"
        "leader = os.getsid(0) == os.getpid();"
        "os.close(os.open('/dev/tty', os.O_RDWR));"  # ENXIO without a ctty
        "print('OK', fg, leader, os.environ.get('MF_PROBE'), os.get_terminal_size(1));"
        "print('PID', os.getpid());"
        "sys.stdout.flush()"
    )
    env = dict(_env(), MF_PROBE="yes")
    argv = (sys.executable, "-c", code)  # any sequence: proc.argv is a list
    proc = mux.spawn_pty(argv, dimensions=(33, 91), env=env)
    assert proc.argv == list(argv)
    assert proc.env is env
    out = _read_all(proc)
    proc.close(force=True)
    # Session leader, its own group, that group in the tty's foreground —
    # what forkpty sets up — and the size from the very first output.
    assert b"OK True True yes os.terminal_size(columns=91, lines=33)" in out, out
    assert ("PID %d" % proc.pid).encode() in out, out


@needs_posix_spawn
def test_no_env_means_the_servers_own(monkeypatch):
    monkeypatch.setenv("MF_PROBE_INHERIT", "inherited")
    proc = mux.spawn_pty(
        [
            sys.executable,
            "-c",
            "import os;print('ENV', os.environ.get('MF_PROBE_INHERIT'))",
        ]
    )
    assert proc.env is None  # only set when the caller passed one
    out = _read_all(proc)
    proc.close(force=True)
    assert b"ENV inherited" in out, out


@needs_posix_spawn
def test_a_resize_after_the_spawn_sigwinches_the_child():
    code = (
        "import os,signal,sys,time;"
        "signal.signal(signal.SIGWINCH, lambda *a: (print('WINCH', os.get_terminal_size(1)), sys.stdout.flush()));"
        "print('READY');sys.stdout.flush();"
        "time.sleep(10)"
    )
    proc = mux.spawn_pty([sys.executable, "-c", code], dimensions=(24, 80))
    try:
        out = b""
        end = time.time() + 10
        while b"READY" not in out:
            assert time.time() < end, out
            out += os.read(proc.fd, 4096)
        proc.setwinsize(50, 132)
        while b"WINCH" not in out or b"lines=50" not in out:
            assert time.time() < end, out
            out += os.read(proc.fd, 4096)
        assert b"WINCH os.terminal_size(columns=132, lines=50)" in out, out
    finally:
        proc.terminate(force=True)
        proc.close(force=True)


@needs_posix_spawn
def test_a_missing_command_raises_like_ptyprocess():
    with pytest.raises(FileNotFoundError, match="not found or was not executable"):
        mux.spawn_pty(["mf-no-such-command-" + uuid.uuid4().hex])


@needs_posix_spawn
def test_a_non_executable_file_raises_ptyprocesss_exact_message(tmp_path):
    script = tmp_path / "not-executable"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o644)
    with pytest.raises(FileNotFoundError) as err:
        mux.spawn_pty([str(script)])
    assert str(err.value) == (
        "The command was not found or was not executable: %s." % script
    )


@needs_posix_spawn
@pytest.mark.parametrize("how", ["bare", "absolute"])
def test_the_command_resolves_by_name_or_by_path(how):
    exe = shutil.which("echo")
    assert exe
    argv = ["echo" if how == "bare" else exe, "mf-resolved-" + how]
    proc = mux.spawn_pty(argv)
    out = _read_all(proc)
    proc.close(force=True)
    assert ("mf-resolved-" + how).encode() in out, out
    assert proc.argv == argv  # argv[0] as given; only the exec path resolves


@needs_posix_spawn
def test_spawning_leaks_no_fds():
    before = set(os.listdir("/proc/self/fd"))
    proc = mux.spawn_pty(["true"])
    _until(lambda: not proc.isalive())
    proc.close(force=True)
    assert set(os.listdir("/proc/self/fd")) <= before


@needs_posix_spawn
def test_a_failed_spawn_closes_both_ends_of_the_pty(monkeypatch):
    def refuse(*a, **kw):
        raise OSError("posix_spawn refused")

    before = set(os.listdir("/proc/self/fd"))
    monkeypatch.setattr(os, "posix_spawn", refuse)
    with pytest.raises(OSError, match="posix_spawn refused"):
        mux.spawn_pty(["true"])
    assert set(os.listdir("/proc/self/fd")) <= before


def _inheritable_fds() -> set:
    """This process's fds that a child WOULD inherit (not close-on-exec) —
    what the test runner itself was handed, not ours to judge."""
    import fcntl

    out = set()
    for name in os.listdir("/proc/self/fd"):
        try:
            if not fcntl.fcntl(int(name), fcntl.F_GETFD) & fcntl.FD_CLOEXEC:
                out.add(int(name))
        except OSError:
            pass
    return out


@needs_posix_spawn
def test_concurrent_spawns_never_leak_one_childs_pty_into_another():
    import threading

    inherited = _inheritable_fds()
    procs: list = []
    errors: list = []
    lock = threading.Lock()
    go = threading.Barrier(6)

    def one():
        try:
            go.wait(5)
            p = mux.spawn_pty(["sleep", "10"])
            with lock:
                procs.append(p)
        except BaseException as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=one) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    try:
        assert not errors, errors
        assert len(procs) == 6
        for p in procs:
            # The fds after exec: 0/1/2 on its own slave, plus whatever the
            # runner handed this process inheritable — never another's PTY.
            fds = {int(n) for n in os.listdir("/proc/%d/fd" % p.pid)}
            assert fds - inherited <= {0, 1, 2}, (p.pid, fds)
            mine = os.readlink("/proc/%d/fd/0" % p.pid)
            for fd in fds - {0, 1, 2}:
                target = os.readlink("/proc/%d/fd/%d" % (p.pid, fd))
                assert "ptmx" not in target and not target.startswith(
                    "/dev/pts/"
                ), target
            assert os.readlink("/proc/%d/fd/1" % p.pid) == mine
            assert os.readlink("/proc/%d/fd/2" % p.pid) == mine
        ttys = [os.readlink("/proc/%d/fd/0" % p.pid) for p in procs]
        assert len(set(ttys)) == 6  # each its own slave
    finally:
        for p in procs:
            p.terminate(force=True)
            p.close(force=True)


def test_off_linux_it_is_ptyprocess_spawn_itself(monkeypatch):
    import ptyprocess

    calls = []

    def spawn(argv, **kw):
        calls.append((argv, kw))
        return "ptyprocess-proc"

    monkeypatch.setattr(mux, "_POSIX_SPAWN_PTY", False)
    monkeypatch.setattr(ptyprocess.PtyProcess, "spawn", spawn)
    env = {"TERM": "xterm"}
    got = mux.spawn_pty(("tmux", "attach"), dimensions=(30, 100), env=env)
    assert got == "ptyprocess-proc"
    assert calls == [(["tmux", "attach"], {"dimensions": (30, 100), "env": env})]
