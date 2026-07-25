"""Terminal plumbing extras: root-wheel forwarding in ``apply_scroll_speed`` and
the exit-marker helpers (path derivation, read/write/clear, natural-exit policy,
and launch-command wrapping).

Complements ``test_scroll_speed.py`` (which owns the persistence/clamp and the
copy-mode binding assertions). Here we pin down the *root* wheel-forward string
and the exit-marker machinery. All subprocess calls are mocked; every filesystem
write is confined to a monkeypatched marker dir under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest

from backend.web.core import terminal

_real_os_write = os.write  # captured before any monkeypatch of terminal.os.write


# --------------------------------------------------------------------------- #
# apply_scroll_speed: root wheel forwarding
# --------------------------------------------------------------------------- #
def _patch_apply(monkeypatch, tmp_path):
    """Common wiring: server 'running', persistence isolated, subprocess captured."""
    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    monkeypatch.setattr(terminal, "_tmux_server_running", lambda: True)
    cmds = []

    def fake_run(argv, *a, **k):
        cmds.append(argv)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    return cmds


def test_total_bind_count_is_six(monkeypatch, tmp_path):
    cmds = _patch_apply(monkeypatch, tmp_path)
    terminal.apply_scroll_speed(4)
    # 4 copy-mode binds (2 tables x 2 directions) + 2 root binds.
    assert len(cmds) == 6


def test_root_forward_string_repeats_send_keys_speed_times(monkeypatch, tmp_path):
    cmds = _patch_apply(monkeypatch, tmp_path)
    terminal.apply_scroll_speed(3)

    root_cmds = [c for c in cmds if c[c.index("-T") + 1] == "root"]
    assert len(root_cmds) == 2
    assert {c[4] for c in root_cmds} == {"WheelUpPane", "WheelDownPane"}

    expected_forward = " ; ".join(["send-keys -M"] * 3)
    assert expected_forward == "send-keys -M ; send-keys -M ; send-keys -M"

    for c in root_cmds:
        assert "if-shell" in c
        assert c[c.index("-F") + 1] == "#{||:#{pane_in_mode},#{mouse_any_flag}}"
        # The "then" branch is the forward string (immediately after the -F cond).
        forward = c[c.index("-F") + 2]
        assert forward == expected_forward


def test_root_otherwise_branches(monkeypatch, tmp_path):
    """WheelUp falls back to copy-mode entry; WheelDown forwards a single wheel."""
    cmds = _patch_apply(monkeypatch, tmp_path)
    terminal.apply_scroll_speed(2)

    root_cmds = [c for c in cmds if c[c.index("-T") + 1] == "root"]
    by_key = {c[4]: c for c in root_cmds}
    # last element is the "otherwise" branch of if-shell
    assert by_key["WheelUpPane"][-1] == "copy-mode -e"
    assert by_key["WheelDownPane"][-1] == "send-keys -M"


def test_speed_one_forward_is_single_send(monkeypatch, tmp_path):
    cmds = _patch_apply(monkeypatch, tmp_path)
    terminal.apply_scroll_speed(1)
    root_cmds = [c for c in cmds if c[c.index("-T") + 1] == "root"]
    for c in root_cmds:
        assert c[c.index("-F") + 2] == "send-keys -M"  # speed==1 -> tmux default


def test_apply_uses_persisted_speed_when_none(monkeypatch, tmp_path):
    """speed=None loads the persisted value and clamps/forwards accordingly
    (a stale out-of-range value like 5 clamps to the new max of 3)."""
    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    (tmp_path / "scroll-speed").write_text("5", encoding="utf-8")
    monkeypatch.setattr(terminal, "_tmux_server_running", lambda: True)
    cmds = []
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda argv, *a, **k: cmds.append(argv) or type("R", (), {"returncode": 0})(),
    )
    terminal.apply_scroll_speed(None)

    root_cmds = [c for c in cmds if c[c.index("-T") + 1] == "root"]
    for c in root_cmds:
        assert c[c.index("-F") + 2] == " ; ".join(["send-keys -M"] * 3)
    # copy-mode -N count also reflects the (clamped) persisted value
    copy_cmds = [
        c for c in cmds if c[c.index("-T") + 1] in ("copy-mode", "copy-mode-vi")
    ]
    for c in copy_cmds:
        assert c[c.index("-N") + 1] == "3"


# --------------------------------------------------------------------------- #
# Exit-marker helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def marker_dir(monkeypatch, tmp_path):
    d = tmp_path / "exit-markers"
    monkeypatch.setattr(terminal, "_EXIT_MARKER_DIR", d)
    return d


def test_exit_marker_path_under_dir_and_suffix(marker_dir):
    p = terminal._exit_marker_path("sc-abc123")
    assert p == marker_dir / "sc-abc123.code"
    assert p.parent == marker_dir


def test_exit_marker_path_sanitizes_unsafe_chars(marker_dir):
    p = terminal._exit_marker_path("weird/name:with spaces!")
    # Everything outside [A-Za-z0-9_.-] becomes '_'.
    assert p.name == "weird_name_with_spaces_.code"
    assert p.parent == marker_dir
    # Path traversal chars are neutralized (no separators leak through).
    assert "/" not in p.name
    assert p.parent == marker_dir


def test_exit_marker_path_preserves_safe_chars(marker_dir):
    p = terminal._exit_marker_path("A_b.c-123")
    assert p.name == "A_b.c-123.code"


def test_read_exit_marker_missing_returns_none(marker_dir):
    assert terminal._read_exit_marker("nope") is None


def test_read_exit_marker_reads_int(marker_dir):
    marker_dir.mkdir(parents=True, exist_ok=True)
    terminal._exit_marker_path("s1").write_text("0\n", encoding="utf-8")
    assert terminal._read_exit_marker("s1") == 0

    terminal._exit_marker_path("s2").write_text("  137  ", encoding="utf-8")
    assert terminal._read_exit_marker("s2") == 137


def test_read_exit_marker_nonint_returns_none(marker_dir):
    marker_dir.mkdir(parents=True, exist_ok=True)
    terminal._exit_marker_path("bad").write_text("not-a-number", encoding="utf-8")
    assert terminal._read_exit_marker("bad") is None


def test_read_exit_marker_empty_returns_none(marker_dir):
    marker_dir.mkdir(parents=True, exist_ok=True)
    terminal._exit_marker_path("empty").write_text("", encoding="utf-8")
    assert terminal._read_exit_marker("empty") is None


def test_clear_exit_marker_removes_file(marker_dir):
    marker_dir.mkdir(parents=True, exist_ok=True)
    p = terminal._exit_marker_path("gone")
    p.write_text("0", encoding="utf-8")
    assert p.exists()
    terminal._clear_exit_marker("gone")
    assert not p.exists()


def test_clear_exit_marker_missing_is_noop(marker_dir):
    # No file present, no directory even: must not raise.
    terminal._clear_exit_marker("never")
    assert not terminal._exit_marker_path("never").exists()


# --------------------------------------------------------------------------- #
# Relaunch decision: _is_natural_exit
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", [0, 130])
def test_is_natural_exit_true(code):
    assert terminal._is_natural_exit(code) is True


@pytest.mark.parametrize("code", [1, 2, 137, 143, 255, -1, None])
def test_is_natural_exit_false(code):
    assert terminal._is_natural_exit(code) is False


def test_relaunch_decision_end_to_end(marker_dir):
    """Round-trip: a session that recorded a clean quit reads back as natural
    (=> do not resume); a killed session (137, or no marker) reads unnatural."""
    marker_dir.mkdir(parents=True, exist_ok=True)

    terminal._exit_marker_path("clean").write_text("0", encoding="utf-8")
    assert terminal._is_natural_exit(terminal._read_exit_marker("clean")) is True

    terminal._exit_marker_path("killed").write_text("137", encoding="utf-8")
    assert terminal._is_natural_exit(terminal._read_exit_marker("killed")) is False

    # No marker at all -> read None -> not natural -> safe to resume.
    assert terminal._read_exit_marker("crashed") is None
    assert terminal._is_natural_exit(terminal._read_exit_marker("crashed")) is False


# --------------------------------------------------------------------------- #
# _wrap_launch_cmd
# --------------------------------------------------------------------------- #
def test_wrap_launch_cmd_shape(marker_dir):
    wrapped = terminal._wrap_launch_cmd("claude --resume", "sess1")
    marker = terminal._exit_marker_path("sess1")
    q = str(marker)
    # Clears stale marker first, runs cmd, then records $? -> marker.
    assert wrapped == "rm -f %s; claude --resume; echo $? > %s" % (q, q)
    # The marker's parent dir gets created as a side effect.
    assert marker.parent.exists()


def test_wrap_launch_cmd_quotes_marker_with_spaces(monkeypatch, tmp_path):
    d = tmp_path / "dir with spaces" / "markers"
    monkeypatch.setattr(terminal, "_EXIT_MARKER_DIR", d)
    wrapped = terminal._wrap_launch_cmd("run-agent", "s")
    marker = terminal._exit_marker_path("s")
    # shlex.quote wraps a path containing spaces in single quotes.
    assert ("'%s'" % marker) in wrapped
    assert wrapped.startswith("rm -f '")
    assert wrapped.endswith("'")


def test_wrap_launch_cmd_sanitized_session_name(marker_dir):
    wrapped = terminal._wrap_launch_cmd("cmd", "a/b")
    # Session name is sanitized in the marker path referenced by the wrapper.
    assert "a_b.code" in wrapped


# --------------------------------------------------------------------------- #
# _tmux_server_running
# --------------------------------------------------------------------------- #
def _fake_run(returncode):
    class R:
        pass

    R.returncode = returncode
    return lambda *a, **k: R()


def test_tmux_server_running_true(monkeypatch):
    monkeypatch.setattr(terminal.subprocess, "run", _fake_run(0))
    assert terminal._tmux_server_running() is True


def test_tmux_server_running_false_when_nonzero(monkeypatch):
    monkeypatch.setattr(terminal.subprocess, "run", _fake_run(1))
    assert terminal._tmux_server_running() is False


def test_tmux_server_running_timeout_is_false(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=10)

    monkeypatch.setattr(terminal.subprocess, "run", boom)
    assert terminal._tmux_server_running() is False


def test_apply_scroll_speed_swallows_binding_timeouts(monkeypatch, tmp_path):
    # A wedged tmux (every bind-key times out) must not propagate out of the
    # best-effort tuning — startup keeps going.
    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    monkeypatch.setattr(terminal, "_tmux_server_running", lambda: True)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=10)

    monkeypatch.setattr(terminal.subprocess, "run", boom)
    terminal.apply_scroll_speed(2)  # no raise


# --------------------------------------------------------------------------- #
# pump_pty: the PTY<->websocket bridge (fd operations mocked; no real tmux)
# --------------------------------------------------------------------------- #
class _FakeProc:
    def __init__(self, fd):
        self.fd = fd
        self.winsize = None
        self.terminated = False

    def setwinsize(self, rows, cols):
        self.winsize = (rows, cols)

    def terminate(self, force=False):
        self.terminated = True


class _FakeWS:
    """A websocket that yields scripted client frames, then blocks until the
    server has flushed PTY output before signalling disconnect (so the assertion
    on forwarded output is race-free)."""

    def __init__(self, frames, sent):
        self._frames = list(frames)
        self._sent = sent
        self.closed = False

    async def receive(self):
        if self._frames:
            await asyncio.sleep(0)  # let the reader/sender tasks run
            return self._frames.pop(0)
        # Drain: wait until the PTY output has been forwarded, then disconnect.
        for _ in range(200):
            if self._sent:
                break
            await asyncio.sleep(0.005)
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data):
        self._sent.append(data)

    async def close(self):
        self.closed = True


async def test_pump_pty_forwards_output_and_input(monkeypatch):
    r, w = os.pipe()
    _real_os_write(w, b"pty-output")
    os.close(w)  # EOF right after the data

    written = []
    monkeypatch.setattr(
        terminal.os, "write", lambda fd, data: written.append((fd, data))
    )
    proc = _FakeProc(r)
    sent: list = []
    frames = [
        {"type": "websocket.receive", "bytes": b"typed"},
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "resize", "rows": 30, "cols": 100}),
        },
        {"type": "websocket.receive", "text": "plain"},
    ]
    ws = _FakeWS(frames, sent)

    await terminal.pump_pty(ws, proc, allow_input=True)

    assert b"pty-output" in b"".join(sent)  # PTY output reached the socket
    assert (r, b"typed") in written  # raw bytes written to the PTY
    assert (r, b"plain") in written  # text input encoded + written
    assert proc.winsize == (30, 100)  # resize frame applied to the PTY
    assert proc.terminated  # torn down in finally
    os.close(r)


async def test_pump_pty_read_only_drops_input(monkeypatch):
    r, w = os.pipe()
    os.close(w)  # immediate EOF; no output needed for this case

    written = []
    monkeypatch.setattr(
        terminal.os, "write", lambda fd, data: written.append((fd, data))
    )
    proc = _FakeProc(r)
    sent: list = []
    frames = [
        {"type": "websocket.receive", "bytes": b"nope"},
        {"type": "websocket.receive", "text": "also-nope"},
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "resize", "rows": 10, "cols": 20}),
        },
    ]
    ws = _FakeWS(frames, sent)

    await terminal.pump_pty(ws, proc, allow_input=False)

    assert written == []  # read-only: neither bytes nor text are written
    assert proc.winsize == (10, 20)  # resize is still honored when read-only
    assert proc.terminated
    os.close(r)


async def test_pump_pty_ignores_malformed_resize_json(monkeypatch):
    r, w = os.pipe()
    os.close(w)
    monkeypatch.setattr(terminal.os, "write", lambda fd, data: None)
    proc = _FakeProc(r)
    sent: list = []
    # Invalid JSON on a read-only socket: no resize, no crash.
    frames = [{"type": "websocket.receive", "text": "{not json"}]
    ws = _FakeWS(frames, sent)
    await terminal.pump_pty(ws, proc, allow_input=False)
    assert proc.winsize is None
    os.close(r)


async def test_pump_pty_swallows_bad_resize_dimensions(monkeypatch):
    # A resize frame with non-integer dims must not crash the pump.
    r, w = os.pipe()
    os.close(w)
    monkeypatch.setattr(terminal.os, "write", lambda fd, data: None)

    class _RaisingProc(_FakeProc):
        def setwinsize(self, rows, cols):
            raise ValueError("bad dims")

    proc = _RaisingProc(r)
    sent: list = []
    frames = [
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "resize", "rows": "x", "cols": "y"}),
        }
    ]
    ws = _FakeWS(frames, sent)
    await terminal.pump_pty(ws, proc, allow_input=True)  # no raise
    assert proc.terminated
    os.close(r)
