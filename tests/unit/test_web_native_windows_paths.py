"""The web layer's native-Windows paths, exercised on any OS (docs/windows.md).

On Windows a session terminal is a :class:`backend.session.mux.Terminal`
whose ``fd`` is ``None`` (the Proactor loop has no ``add_reader``): the pump
reads it from a thread and writes through ``proc.write``. Every tmux argv
goes through the mux door, every attach through ``mux.attach``, the file
locks through ``backend.compat``; ``tail -F``, ``ps`` and directory fds have
stand-ins. Each is driven here with fakes so Linux CI pins them.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import threading
import time

import pytest

from backend import compat
from backend.session import mux
from backend.web.core import mailbox, proc, pty_run, terminal, uploads


# --------------------------------------------------------------------------- #
# a Terminal without an fd
# --------------------------------------------------------------------------- #
class _FdlessTerminal:
    """What ``mux.attach`` returns on Windows: no fd, a blocking ``read``
    (``b""`` once closed) and ``write``."""

    fd = None
    pid = None

    def __init__(self, chunks):
        self._q: "queue.Queue" = queue.Queue()
        for c in chunks:
            self._q.put(c)
        self.written = []
        self.winsize = None
        self.terminated = False

    def feed(self, data: bytes) -> None:
        self._q.put(data)

    def read(self, size=1024):
        item = self._q.get()
        if item is None:
            self._q.put(None)  # stays closed for any later read
            return b""
        return item

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def setwinsize(self, rows, cols):
        self.winsize = (rows, cols)

    def terminate(self, force=False):
        self.terminated = True
        self._q.put(None)
        return True


class _WS:
    """Scripted client frames; disconnects once ``until()`` holds."""

    def __init__(self, frames, until):
        self._frames = list(frames)
        self._until = until
        self.sent = []
        self.closed = False

    async def receive(self):
        if self._frames:
            await asyncio.sleep(0.01)
            return self._frames.pop(0)
        for _ in range(400):
            if self._until(self):
                break
            await asyncio.sleep(0.005)
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data):
        self.sent.append(data)

    async def send_text(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


async def test_pump_reads_an_fdless_terminal_in_a_thread_and_writes_through_it(
    monkeypatch,
):
    def no_add_reader(*a, **k):  # the Proactor loop's: there is none
        raise NotImplementedError

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_reader", no_add_reader)
    pty = _FdlessTerminal([b"hello ", b"world"])
    ws = _WS(
        [
            {"type": "websocket.receive", "bytes": b"ls\r"},
            {"type": "websocket.receive", "text": "q"},
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "resize", "rows": 30, "cols": 100}),
            },
        ],
        until=lambda w: b"".join(x for x in w.sent if isinstance(x, bytes))
        == b"hello world",
    )
    await terminal.pump_pty(ws, pty, allow_input=True)
    assert b"".join(x for x in ws.sent if isinstance(x, bytes)) == b"hello world"
    assert pty.written == [b"ls\r", b"q"]
    assert pty.winsize == (30, 100)
    assert pty.terminated


async def test_pump_closes_the_socket_when_the_fdless_terminal_ends():
    pty = _FdlessTerminal([b"bye", None])
    ws = _WS([], until=lambda w: w.closed)
    await terminal.pump_pty(ws, pty, allow_input=False)
    assert ws.sent == [b"bye"]
    assert ws.closed


async def test_start_pty_reader_thread_feeds_the_queue_then_none():
    loop = asyncio.get_running_loop()
    q: "asyncio.Queue" = asyncio.Queue()
    pty = _FdlessTerminal([b"a", "b", None])  # a str chunk is encoded
    stop = terminal.start_pty_reader(loop, pty, q)
    got = [await asyncio.wait_for(q.get(), 2) for _ in range(3)]
    stop()
    assert got == [b"a", b"b", None]


async def test_start_pty_reader_stop_ends_the_thread_without_a_sentinel():
    loop = asyncio.get_running_loop()
    q: "asyncio.Queue" = asyncio.Queue()
    pty = _FdlessTerminal([])
    before = {t.ident for t in threading.enumerate()}
    stop = terminal.start_pty_reader(loop, pty, q)
    stop()
    pty.feed(b"late")  # unblocks the read; dropped after stop
    for _ in range(200):
        alive = [
            t
            for t in threading.enumerate()
            if t.name == "pty-reader" and t.ident not in before
        ]
        if not alive:
            break
        await asyncio.sleep(0.005)
    assert not alive
    assert q.empty()


def test_pty_write_uses_the_fd_when_there_is_one(monkeypatch):
    seen = []
    monkeypatch.setattr(terminal.os, "write", lambda fd, b: seen.append((fd, b)))

    class P:
        fd = 7

    terminal.pty_write(P(), b"x")
    pty = _FdlessTerminal([])
    terminal.pty_write(pty, b"y")
    assert seen == [(7, b"x")]
    assert pty.written == [b"y"]


# --------------------------------------------------------------------------- #
# the mux door
# --------------------------------------------------------------------------- #
def test_attach_goes_through_mux(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mux, "attach", lambda name, **kw: calls.append((name, kw)) or "term"
    )
    from backend.web.core import guest_terminal

    assert terminal.spawn_tmux_attach("mf_a", dimensions=(30, 90)) == "term"
    assert guest_terminal.spawn_readonly_attach("mf_b", 24, 80) == "term"
    assert calls == [
        ("mf_a", {"dimensions": (30, 90)}),
        ("=mf_b", {"readonly": True, "dimensions": (24, 80)}),
    ]


def test_mux_attach_on_posix_is_the_same_tmux_client(monkeypatch):
    ptyprocess = pytest.importorskip("ptyprocess")
    if mux.IS_WINDOWS:
        pytest.skip("POSIX attach")
    seen = {}

    def spawn(argv, dimensions, env):
        seen.update(argv=argv, dimensions=dimensions, term=env["TERM"])
        return "proc"

    monkeypatch.setattr(ptyprocess.PtyProcess, "spawn", spawn)
    assert terminal.spawn_tmux_attach("mf_x") == "proc"
    assert seen == {
        "argv": ["tmux", "attach-session", "-t", "mf_x"],
        "dimensions": (24, 80),
        "term": "xterm-256color",
    }


def test_run_capped_routes_only_tmux_argv_through_mux(monkeypatch):
    via_mux, via_sp = [], []

    def fake_mux(argv, **kw):
        via_mux.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def fake_sp(argv, **kw):
        via_sp.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(proc.mux, "run", fake_mux)
    monkeypatch.setattr(proc.subprocess, "run", fake_sp)
    proc.run_capped(["tmux", "has-session", "-t=x"], timeout=1)
    proc.run_capped(["git", "status"], timeout=1)
    assert via_mux == [["tmux", "has-session", "-t=x"]]
    assert via_sp == [["git", "status"]]


def test_run_capped_keeps_the_timeout_contract_through_mux(monkeypatch):
    def hung(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"], output=b"partial")

    monkeypatch.setattr(proc.mux, "run", hung)
    cp = proc.run_capped(["tmux", "capture-pane", "-p"], timeout=2)
    assert cp.returncode == 124
    assert cp.stdout.startswith(b"partial\ntimed out after 2s: tmux capture-pane")


def test_shell_is_unchanged_off_windows():
    if compat.IS_WINDOWS:
        pytest.skip("POSIX shell")
    assert proc.shell() == "sh"
    assert proc.shell("bash") == "bash"


# --------------------------------------------------------------------------- #
# compat locks
# --------------------------------------------------------------------------- #
def test_mailbox_write_lock_is_a_compat_flock(monkeypatch, tmp_path):
    monkeypatch.setattr(mailbox, "mailbox_path", lambda: str(tmp_path / "mb.json"))
    ops = []
    real = compat.flock
    monkeypatch.setattr(compat, "flock", lambda fd, op: ops.append(op) or real(fd, op))
    with mailbox._write_lock():
        pass
    assert ops == [compat.LOCK_EX]
    assert (tmp_path / "mb.json.lock").exists()


# --------------------------------------------------------------------------- #
# tail -F without tail
# --------------------------------------------------------------------------- #
def _read_until(f, want: bytes, timeout=3.0) -> bytes:
    out = b""
    deadline = time.monotonic() + timeout
    while want not in out and time.monotonic() < deadline:
        box = []
        t = threading.Thread(target=lambda: box.append(f.read(4096)), daemon=True)
        t.start()
        t.join(timeout=max(0.0, deadline - time.monotonic()))
        if box:
            out += box[0]
    return out


def test_file_follower_tails_last_lines_then_follows_and_truncation(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"one\ntwo\nthree\n")
    f = terminal.FileFollower(log, lines=2)
    f.POLL_S = 0.01
    assert f.fd is None
    assert _read_until(f, b"three\r\n") == b"two\r\nthree\r\n"
    with open(log, "ab") as fh:
        fh.write(b"four\n")
    assert _read_until(f, b"four\r\n") == b"four\r\n"
    log.write_bytes(b"new\n")  # truncated + rewritten: from the top again
    assert _read_until(f, b"new\r\n") == b"new\r\n"
    f.terminate()
    assert f.read() == b""
    assert not f.isalive()


def test_file_follower_waits_for_a_missing_file(tmp_path):
    log = tmp_path / "later.log"
    f = terminal.FileFollower(log, lines=5)
    f.POLL_S = 0.01
    log.write_bytes(b"hi\n")
    assert _read_until(f, b"hi\r\n") == b"hi\r\n"
    f.terminate()


def test_spawn_tail_is_a_follower_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal.mux, "IS_WINDOWS", True)
    f = terminal.spawn_tail(tmp_path / "y.log", lines=3)
    assert isinstance(f, terminal.FileFollower)
    f.terminate()


# --------------------------------------------------------------------------- #
# ps without ps
# --------------------------------------------------------------------------- #
def test_ps_table_has_ps_shape_with_this_process_in_it():
    pytest.importorskip("psutil")
    cp = proc.ps_table("pid=,ppid=,comm=")
    assert cp.returncode == 0
    rows = {}
    for line in cp.stdout.decode().splitlines():
        pid, ppid, comm = line.split(None, 2)
        rows[pid] = (ppid, comm)
    me = rows[str(os.getpid())]
    assert me[0] == str(os.getppid())
    assert not me[1].lower().endswith(".exe")
    args = proc.ps_table("pid=,ppid=,args=").stdout.decode().splitlines()
    mine = [ln for ln in args if ln.split(None, 1)[0] == str(os.getpid())]
    assert mine and "python" in mine[0].lower()


def test_ps_tokens_keep_a_path_with_blanks_whole():
    assert proc._ps_token(r"C:\Program Files\nodejs\node.exe") == (
        r"C:\Program_Files\nodejs\node.exe"
    )


def test_agent_tree_walk_reads_ps_table_on_windows(monkeypatch):
    from backend.web.core import agent_state

    monkeypatch.setattr(agent_state.compat, "IS_WINDOWS", True)
    monkeypatch.setattr(agent_state, "_PID_TREE_CACHE", {})
    table = b"10 1 bash\n11 10 claude\n20 1 bash\n21 20 bash\n"
    monkeypatch.setattr(
        agent_state._proc,
        "ps_table",
        lambda cols: subprocess.CompletedProcess(["ps"], 0, stdout=table),
    )
    monkeypatch.setattr(
        agent_state._proc,
        "run_capped",
        lambda *a, **k: pytest.fail("ps must not run on Windows"),
    )
    assert agent_state._pane_has_agent_process(10) is True
    assert agent_state._pane_has_agent_process(20) is False


# --------------------------------------------------------------------------- #
# pastes without directory fds
# --------------------------------------------------------------------------- #
def test_paste_by_path_writes_prunes_and_refuses_a_linked_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(uploads, "_FD_DIRS", False)
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(uploads._PASTE_KEEP + 3):
        p = uploads.write_workspace_paste(str(ws), "paste-%02d.png" % i, b"x")
        os.utime(p, (i, i))
    left = sorted(os.listdir(ws / uploads.WORKSPACE_PASTE_DIR))
    assert len(left) == uploads._PASTE_KEEP
    assert left[-1] == "paste-%02d.png" % (uploads._PASTE_KEEP + 2)
    with pytest.raises(OSError):
        uploads.write_workspace_paste(str(ws), left[-1], b"again")  # O_EXCL

    other = tmp_path / "ws2"
    other.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, other / uploads.WORKSPACE_PASTE_DIR)
    except OSError:  # Windows without the symlink privilege
        pytest.skip("cannot make a symlink here")
    with pytest.raises(OSError):
        uploads.write_workspace_paste(str(other), "paste-1.png", b"x")
    assert os.listdir(outside) == []
    (outside / "paste-0.png").write_bytes(b"keep")
    uploads._prune_pastes(str(other / uploads.WORKSPACE_PASTE_DIR), keep=0)
    assert os.listdir(outside) == ["paste-0.png"]


# --------------------------------------------------------------------------- #
# a plain-PTY run without an fd (a ConPTY)
# --------------------------------------------------------------------------- #
def test_pty_run_drains_and_writes_an_fdless_pty(monkeypatch):
    pty = _FdlessTerminal([b"installing", b"...", None])
    pty.wait = lambda: 0
    monkeypatch.setattr(pty_run, "_spawn", lambda argv, cwd, env: pty)
    run = pty_run.PtyRun(["sh", "-c", "true"], "/")
    for _ in range(400):
        if run.finished:
            break
        time.sleep(0.005)
    assert run.finished
    assert run.attach(lambda d: None) == b"installing..."
    run.finished = False
    run.write(b"y\r")
    assert pty.written == [b"y\r"]


# --------------------------------------------------------------------------- #
# user event hooks without shebangs; du without du
# --------------------------------------------------------------------------- #
def test_hook_argv_is_the_file_itself_off_windows(tmp_path):
    from backend.web.core import events

    if compat.IS_WINDOWS:
        pytest.skip("POSIX hooks")
    hook = tmp_path / "notify.sh"
    assert events._hook_argv(hook) == [str(hook)]


def test_hook_argv_on_windows_picks_the_interpreter(monkeypatch, tmp_path):
    from backend.web.core import events

    monkeypatch.setattr(events.compat, "IS_WINDOWS", True)
    monkeypatch.setattr(events.compat, "posix_shell", lambda bash=False: "bash.exe")
    assert events._hook_argv(tmp_path / "a.cmd") == [str(tmp_path / "a.cmd")]
    assert events._hook_argv(tmp_path / "a.EXE") == [str(tmp_path / "a.EXE")]
    assert events._hook_argv(tmp_path / "a.ps1")[-2:] == [
        "-File",
        str(tmp_path / "a.ps1"),
    ]
    assert events._hook_argv(tmp_path / "notify") == [
        "bash.exe",
        str(tmp_path / "notify"),
    ]


def test_workspace_size_walk_matches_the_files_and_skips_links(monkeypatch, tmp_path):
    from backend.web.core import workspaces

    (tmp_path / "a").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"y" * 23)
    big = tmp_path.parent / (tmp_path.name + "-outside")
    big.mkdir()
    (big / "huge").write_bytes(b"z" * 5000)
    try:
        os.symlink(big, tmp_path / "link")
    except OSError:  # Windows without the symlink privilege
        pass
    monkeypatch.setattr(workspaces.compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        workspaces._proc, "run_capped", lambda *a, **k: pytest.fail("no du on Windows")
    )
    assert workspaces._dir_size_bytes(str(tmp_path)) == 123
