"""The native-Windows branches of the backend outside web (docs/windows.md),
pinned on Linux by flipping the modules' own platform switches: tmux argv go
through ``backend.session.mux``, locks are ``backend.compat``'s, and nothing
probes a pid with ``os.kill(pid, 0)`` (which terminates it on Windows)."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from backend import cmd as cmd_pkg
from backend import compat, osflags
from backend.session import mux


# --------------------------------------------------------------------------- #
# osflags
# --------------------------------------------------------------------------- #
def test_posix_flags_are_the_os_values():
    assert osflags.O_NOFOLLOW == os.O_NOFOLLOW
    assert osflags.O_CLOEXEC == os.O_CLOEXEC
    assert osflags.O_NONBLOCK == os.O_NONBLOCK
    assert osflags.O_DIRECTORY == os.O_DIRECTORY


def test_pid_alive_never_signals_on_windows(monkeypatch):
    monkeypatch.setattr(osflags, "IS_WINDOWS", True)
    asked = []
    monkeypatch.setattr(
        osflags, "_win_pid_alive", lambda pid: asked.append(pid) or True
    )
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("os.kill on Windows"))
    assert osflags.pid_alive(os.getpid()) is True
    assert asked == [os.getpid()]
    assert osflags.pid_alive(0) is False
    assert osflags.pid_alive("x") is False


def test_windows_pid_probe_is_psutil():
    assert osflags._win_pid_alive(os.getpid()) is True
    assert osflags._win_pid_alive(2**22 + 12345) is False


def test_pid_alive_posix(monkeypatch):
    assert osflags.pid_alive(os.getpid()) is True
    assert osflags.pid_alive(2**22 + 12345) is False


def test_tmux_run_goes_to_the_session_host_on_windows(monkeypatch):
    seen = []
    monkeypatch.setattr(osflags, "IS_WINDOWS", True)
    monkeypatch.setattr(
        mux, "run", lambda argv, **kw: seen.append((list(argv), kw)) or "CP"
    )
    assert osflags.tmux_run(["tmux", "ls"], capture_output=True) == "CP"
    assert seen == [(["tmux", "ls"], {"capture_output": True})]


def test_tmux_exec_on_windows_answers_like_a_process(monkeypatch):
    monkeypatch.setattr(osflags, "IS_WINDOWS", True)
    calls = []

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, 1, b"out", b"err")

    monkeypatch.setattr(mux, "run", fake_run)

    async def go():
        p = await osflags.tmux_exec(
            "tmux",
            "has-session",
            "-t",
            "x",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return p.returncode, await p.communicate(), await p.wait()

    rc, (out, err), waited = asyncio.run(go())
    assert (rc, out, err, waited) == (1, b"out", b"err", 1)
    argv, kw = calls[0]
    assert argv == ["tmux", "has-session", "-t", "x"]
    assert kw["stdout"] == subprocess.PIPE and kw["stderr"] == subprocess.DEVNULL


# --------------------------------------------------------------------------- #
# The engine: cmd Executor + PTY factory
# --------------------------------------------------------------------------- #
def test_exec_routes_tmux_argv_through_mux(monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"hi", b"")

    monkeypatch.setattr(mux, "run", fake_run)
    out, err = cmd_pkg.Exec().output(cmd_pkg.command("tmux", "ls"))
    assert (out, err, seen) == (b"hi", None, [["tmux", "ls"]])
    assert cmd_pkg.Exec().run(cmd_pkg.command("tmux", "has-session")) is None


def test_exec_runs_other_programs_directly(monkeypatch):
    monkeypatch.setattr(mux, "run", lambda *a, **k: pytest.fail("not a tmux argv"))
    out, err = cmd_pkg.Exec().output(cmd_pkg.command("echo", "x"))
    assert err is None and out.strip() == b"x"


class _FakeTerm:
    fd = None
    pid = None

    def __init__(self):
        self.written, self.sizes, self.closed = [], [], False

    def read(self, size=1024):
        raise EOFError

    def write(self, data):
        self.written.append(data)
        return len(data)

    def setwinsize(self, rows, cols):
        self.sizes.append((rows, cols))

    def isalive(self):
        return not self.closed

    def close(self, force=True):
        self.closed = True


def test_pty_attach_on_windows_is_a_session_host_client(monkeypatch):
    from backend.session.tmux import pty as pty_pkg

    term = _FakeTerm()
    attached = []
    monkeypatch.setattr(pty_pkg, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        mux,
        "attach",
        lambda s, readonly=False, **kw: attached.append((s, readonly)) or term,
    )
    f, err = pty_pkg.Pty().start(
        cmd_pkg.command("tmux", "attach-session", "-t", "mindflock_x")
    )
    assert err is None and attached == [("mindflock_x", False)]
    assert f.fileno() is None
    f.write(b"\r")
    assert term.written == [b"\r"]
    assert f.read() == b""  # EOFError = the client is gone
    assert pty_pkg.set_size(f, pty_pkg.Winsize(rows=40, cols=120)) is None
    assert term.sizes == [(40, 120)]
    f.close()
    assert term.closed


def test_pty_new_session_on_windows_runs_to_completion(monkeypatch):
    from backend.session.tmux import pty as pty_pkg

    calls = []

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(pty_pkg, "_IS_WINDOWS", True)
    monkeypatch.setattr(mux, "run", fake_run)
    c = cmd_pkg.command("tmux", "new-session", "-d", "-s", "s", "-c", "C:/w", "x")
    f, err = pty_pkg.Pty().start(c)
    assert err is None and calls[0][0] == c.args
    f.close()  # nothing to reap

    monkeypatch.setattr(
        mux,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"duplicate"),
    )
    f, err = pty_pkg.Pty().start(c)
    assert f is None and isinstance(err, cmd_pkg.ExitError)
    assert err.exit_code() == 1


def test_pty_on_windows_refuses_non_tmux(monkeypatch):
    from backend.session.tmux import pty as pty_pkg

    monkeypatch.setattr(pty_pkg, "_IS_WINDOWS", True)
    f, err = pty_pkg.Pty().start(cmd_pkg.command("bash"))
    assert f is None and isinstance(err, OSError)


# --------------------------------------------------------------------------- #
# Locks are compat's
# --------------------------------------------------------------------------- #
def test_slot_locks_through_compat(monkeypatch, tmp_path):
    from backend import slot

    ops = []
    real = compat.flock
    monkeypatch.setattr(compat, "flock", lambda fd, op: ops.append(op) or real(fd, op))
    monkeypatch.setattr(slot, "slots_root", lambda: str(tmp_path))
    s = slot.try_acquire("heavy", 1)
    assert s is not None
    assert compat.LOCK_EX | compat.LOCK_NB in ops
    assert slot._probe_locked(s.lock_path) is True
    s.release()
    assert (
        slot._probe_locked(os.path.join(str(tmp_path), "heavy", "slot-0.lock")) is False
    )


def test_slot_waiter_on_windows_never_renames_an_open_file(monkeypatch, tmp_path):
    from backend import slot

    monkeypatch.setattr(slot.osflags, "IS_WINDOWS", True)
    renamed = []
    monkeypatch.setattr(os, "rename", lambda *a: renamed.append(a))
    w = slot._Waiter(str(tmp_path), {"pid": 1})
    assert os.path.exists(w.path) and renamed == []
    assert slot._probe_locked(w.path) is True
    w.close()
    assert not os.path.exists(w.path)


def test_state_lock_is_compat(monkeypatch, tmp_path):
    from backend.config import state

    ops = []
    monkeypatch.setattr(compat, "flock", lambda fd, op: ops.append(op))
    monkeypatch.setattr(state, "GetConfigDir", lambda: str(tmp_path))
    with state.state_file_lock():
        pass
    assert ops == [compat.LOCK_EX]


# --------------------------------------------------------------------------- #
# MCP identity
# --------------------------------------------------------------------------- #
def test_identity_asks_the_session_host_on_windows(monkeypatch):
    from backend.mcp.identity import Identity

    monkeypatch.setattr(osflags, "IS_WINDOWS", True)
    seen = []

    def fake_run(argv, **kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "mindflock_fix\n", "")

    monkeypatch.setattr(mux, "run", fake_run)
    ident = Identity(env={"TMUX": "winmux,4242,0", "TMUX_PANE": "%3"})
    assert ident.tmux_session_name() == "mindflock_fix"
    assert seen == [["tmux", "display-message", "-p", "-t", "%3", "#{session_name}"]]


# --------------------------------------------------------------------------- #
# memory pressure: psutil, no PSI
# --------------------------------------------------------------------------- #
def test_meminfo_from_psutil_on_windows(monkeypatch):
    from backend import memory_pressure

    monkeypatch.setattr(os, "name", "nt")
    try:
        mem = memory_pressure._read_meminfo()
    finally:
        monkeypatch.undo()
    assert mem is not None and mem["total"] > 0
    assert 0 <= mem["available"] <= mem["total"]
    assert set(mem) == {"total", "available", "swap_total", "swap_free"}


# --------------------------------------------------------------------------- #
# Background jobs: psutil process table in /proc's units
# --------------------------------------------------------------------------- #
def test_background_jobs_psutil_table_matches_proc(monkeypatch):
    from backend.providers import claude_background as cb

    me = os.getpid()
    proc_view = cb._read_stat(me)
    monkeypatch.setattr(cb, "_IS_WINDOWS", True)
    ps_view = cb._read_stat(me)
    assert ps_view["ppid"] == proc_view["ppid"] == os.getppid()
    # same clock-tick units, counted from boot (psutil rounds differently)
    assert abs(ps_view["start"] - proc_view["start"]) <= cb._hz() * 2
    assert me in cb._pids()
    assert "python" in cb._cmdline(me).lower() or "pytest" in cb._cmdline(me)
    assert cb._stdout_target(me) == ""
    assert cb._boot_time(0.0) is not None


def test_background_stop_on_windows_kills_the_tree(monkeypatch):
    from backend.providers import claude_background as cb

    monkeypatch.setattr(cb, "_IS_WINDOWS", True)
    monkeypatch.setattr(cb, "_pane_pid", lambda s: "1")
    monkeypatch.setattr(cb, "jobs_under", lambda p: [{"pid": 77}])
    killed = []
    monkeypatch.setattr(
        compat, "kill_group", lambda pid, sig: killed.append((pid, sig))
    )
    monkeypatch.setattr(cb, "_read_stat", lambda pid: None)
    monkeypatch.setattr(cb, "forget", lambda s: None)
    monkeypatch.setattr(
        os, "getpgid", lambda *_: pytest.fail("no process groups on Windows")
    )
    assert cb.stop("s", 77) is True
    assert killed and killed[0][0] == 77


# --------------------------------------------------------------------------- #
# Shared MCP: no os.kill probes, the tree goes on stop
# --------------------------------------------------------------------------- #
def test_mcp_share_pid_alive_on_windows(monkeypatch):
    from backend.providers import mcp_share

    monkeypatch.setattr(mcp_share.compat, "IS_WINDOWS", True)
    monkeypatch.setattr(osflags, "IS_WINDOWS", True)
    monkeypatch.setattr(osflags, "_win_pid_alive", lambda pid: pid == 41)
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("os.kill on Windows"))
    assert mcp_share._pid_alive(41) is True
    assert mcp_share._pid_alive(42) is False


def test_gitutil_group_timeout_kills_through_compat(monkeypatch):
    from backend import gitutil

    killed = []
    real = compat.kill_group
    monkeypatch.setattr(
        compat, "kill_group", lambda pid, sig: killed.append(sig) or real(pid, sig)
    )
    with pytest.raises(subprocess.TimeoutExpired):
        gitutil._run_group(["sleep", "5"], 0.2, {})
    assert killed == [9]


# --------------------------------------------------------------------------- #
# Peer: the shared folder's reads and the contract file without dir_fd
# --------------------------------------------------------------------------- #
def test_share_reads_on_windows_refuse_links(monkeypatch, tmp_path):
    from backend.peer import share as share_mod

    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "a.txt").write_bytes(b"one\ntwo\n")
    (tmp_path / "secret").write_text("nope")
    os.symlink(tmp_path / "secret", work / "src" / "link")
    os.symlink(tmp_path, work / "up")
    monkeypatch.setattr(share_mod.osflags, "IS_WINDOWS", True)
    sh = type("S", (), {"work": str(work)})()
    fd = share_mod._open_regular(sh, ["src", "a.txt"])
    try:
        assert os.read(fd, 100) == b"one\ntwo\n"
    finally:
        os.close(fd)
    for parts in (["src", "link"], ["up", "secret"], ["src"]):
        with pytest.raises(share_mod.ShareError):
            share_mod._open_regular(sh, parts)


def test_contract_file_on_windows_replaces_a_planted_link(monkeypatch, tmp_path):
    from backend.peer import joint

    (tmp_path / "outside").write_text("keep")
    os.symlink(tmp_path / "outside", tmp_path / joint.CONTRACT_FILE)
    work = tmp_path
    monkeypatch.setattr(joint.osflags, "IS_WINDOWS", True)
    joint.write_contract_file(str(work), "the deal\n")
    assert (tmp_path / "outside").read_text() == "keep"
    assert not os.path.islink(tmp_path / joint.CONTRACT_FILE)
    assert (tmp_path / joint.CONTRACT_FILE).read_bytes() == b"the deal\n"


def test_fchmod_and_dir_fsync_are_noops_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(osflags, "IS_WINDOWS", True)
    monkeypatch.setattr(os, "fchmod", lambda *a: pytest.fail("no fchmod there"))
    osflags.fchmod(0, 0o600)
    osflags.fsync_dir(str(tmp_path / "missing"))  # never opens the dir


def test_sandbox_exec_signal_list_is_portable():
    from backend.peer import sandbox_exec

    import signal

    assert signal.SIGWINCH in sandbox_exec.FORWARD_SIGNALS
    assert len(sandbox_exec.FORWARD_SIGNALS) == 7


# --------------------------------------------------------------------------- #
# Shells, homes, sourced secrets
# --------------------------------------------------------------------------- #
def test_setup_commands_run_in_git_bash_on_windows(monkeypatch):
    from backend import workspace_setup

    assert workspace_setup.shell_argv("uv sync && echo ok") is None  # POSIX
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(compat, "posix_shell", lambda bash=False: "C:/Git/bin/bash.exe")
    try:
        argv = workspace_setup.shell_argv("uv sync && echo ok")
    finally:
        monkeypatch.undo()
    assert argv == ["C:/Git/bin/bash.exe", "-c", "uv sync && echo ok"]


def test_config_home_falls_back_to_userprofile_on_windows(monkeypatch):
    from backend.config import config

    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", r"C:\Users\me")
    assert config._user_home_dir() is None  # POSIX: $HOME or nothing
    monkeypatch.setattr(os, "name", "nt")
    try:
        got = config._user_home_dir()
    finally:
        monkeypatch.undo()
    assert got == r"C:\Users\me"


def test_secret_env_source_prefix_uses_forward_slashes_on_windows(monkeypatch):
    from backend.session import secret_env

    assert secret_env.source_prefix("/a/b.env") == ". /a/b.env 2>/dev/null || true\n"
    monkeypatch.setattr(os, "name", "nt")
    try:
        got = secret_env.source_prefix(r"C:\Users\me\.mindflock\run\s.env")
    finally:
        monkeypatch.undo()
    assert got == ". C:/Users/me/.mindflock/run/s.env 2>/dev/null || true\n"


def test_git_paths_are_native_on_windows(monkeypatch):
    from backend import gitutil

    text = "worktree C:/Users/me/repo\nbranch refs/heads/main\n"
    assert gitutil.parse_worktree_list(text)[0]["path"] == "C:/Users/me/repo"
    monkeypatch.setattr(gitutil, "_IS_WINDOWS", True)
    got = gitutil.parse_worktree_list(text)[0]["path"]
    assert got == os.path.normpath("C:/Users/me/repo")
