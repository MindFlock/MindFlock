"""Behavioural tests for :class:`backend.session.tmux.tmux.TmuxSession`.

The whole session is driven through injected dependencies
(:class:`backend.cmd.MockCmdExec` and a fake ``PtyFactory``) so NO real tmux
server, PTY, or subprocess is ever created. We pin:

  * the ``start`` command building (env prefix, session-scoped set-environment)
    and its failure/cleanup paths,
  * ``does_session_exist`` exact-match argv,
  * pane capture argv + error wrapping,
  * keystroke / trust-prompt / change-detection behaviour against a fake ptmx,
  * ``close`` / ``detach_safely`` resource teardown and error combination,
  * ``update_window_size`` masking.

INTENTIONALLY UNCOVERED (noted, not faked): :meth:`TmuxSession.attach` spawns
threads that read the real stdin fd / PTY master and install SIGWINCH via
``platform.monitor_window_size`` — that needs a live tty, so it is exercised
only under a real attach, never here.
"""

from __future__ import annotations

from backend import cmd as cmd_pkg
from backend.session.tmux import pty as pty_pkg
from backend.session.tmux import tmux


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakePtmx:
    """Minimal stand-in for a PtyFile: records writes, tracks close()."""

    def __init__(self):
        self.writes = []
        self.closed = False
        self.raise_on_close = False

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def close(self):
        if self.raise_on_close:
            raise OSError("close boom")
        self.closed = True


class _FakePtyFactory(pty_pkg.PtyFactory):
    """PtyFactory that hands back a canned (ptmx, err) per call."""

    def __init__(self, results):
        # results: list of (ptmx_or_None, err_or_None), consumed in order.
        self._results = list(results)
        self.started_cmds = []

    def start(self, cmd):
        self.started_cmds.append(cmd)
        if self._results:
            return self._results.pop(0)
        return _FakePtmx(), None

    def close(self):
        return None


def _session(
    name="demo", program="claude", pty_results=None, run_func=None, output_func=None
):
    factory = _FakePtyFactory(pty_results or [])
    mock = cmd_pkg.MockCmdExec(run_func=run_func, output_func=output_func)
    sess = tmux.NewTmuxSessionWithDeps(name, program, factory, mock)
    return sess, factory, mock


# ---------------------------------------------------------------------------
# _StatusMonitor.hash
# ---------------------------------------------------------------------------
def test_status_monitor_hash_is_sha256_of_utf8():
    import hashlib

    mon = tmux._new_status_monitor()
    assert mon.prev_output_hash is None
    got = mon.hash("héllo")
    assert got == hashlib.sha256("héllo".encode("utf-8")).digest()


# ---------------------------------------------------------------------------
# does_session_exist — exact-match `-t=<name>`
# ---------------------------------------------------------------------------
def test_does_session_exist_true_when_run_returns_none():
    seen = {}

    def run_func(c):
        seen["args"] = c.args
        return None  # exit 0 -> exists

    sess, _, _ = _session(run_func=run_func)
    assert sess.does_session_exist() is True
    assert seen["args"] == ["tmux", "has-session", "-t=mindflock_demo"]


def test_does_session_exist_false_when_run_errors():
    sess, _, _ = _session(run_func=lambda c: Exception("no such session"))
    assert sess.does_session_exist() is False


# ---------------------------------------------------------------------------
# capture_pane_content
# ---------------------------------------------------------------------------
def test_capture_pane_content_success():
    def output_func(c):
        return b"pane text", None

    sess, _, _ = _session(output_func=output_func)
    content, err = sess.capture_pane_content()
    assert err is None
    assert content == "pane text"


def test_capture_pane_content_argv():
    seen = {}

    def output_func(c):
        seen["args"] = c.args
        return b"", None

    sess, _, _ = _session(output_func=output_func)
    sess.capture_pane_content()
    assert seen["args"] == [
        "tmux",
        "capture-pane",
        "-p",
        "-e",
        "-J",
        "-t",
        "mindflock_demo",
    ]


def test_capture_pane_content_error_wrapped():
    sess, _, _ = _session(output_func=lambda c: (b"", Exception("boom")))
    content, err = sess.capture_pane_content()
    assert content == ""
    assert "error capturing pane content: boom" in str(err)


def test_capture_pane_content_with_options_argv_and_error():
    seen = {}

    def output_func(c):
        seen["args"] = c.args
        return b"", Exception("bad range")

    sess, _, _ = _session(output_func=output_func)
    content, err = sess.capture_pane_content_with_options("-", "-")
    assert seen["args"] == [
        "tmux",
        "capture-pane",
        "-p",
        "-e",
        "-J",
        "-S",
        "-",
        "-E",
        "-",
        "-t",
        "mindflock_demo",
    ]
    assert content == ""
    assert "failed to capture tmux pane content with options: bad range" in str(err)


# ---------------------------------------------------------------------------
# tap_enter / send_keys — write to ptmx
# ---------------------------------------------------------------------------
def test_tap_enter_writes_carriage_return():
    sess, _, _ = _session()
    ptmx = _FakePtmx()
    sess.ptmx = ptmx
    assert sess.tap_enter() is None
    assert ptmx.writes == [bytes([0x0D])]


def test_tap_enter_error_returns_exception():
    sess, _, _ = _session()

    class _Bad:
        def write(self, _):
            raise OSError("pty gone")

    sess.ptmx = _Bad()
    err = sess.tap_enter()
    assert "error sending enter keystroke to PTY: pty gone" in str(err)


def test_send_keys_writes_utf8():
    sess, _, _ = _session()
    ptmx = _FakePtmx()
    sess.ptmx = ptmx
    assert sess.send_keys("hí") is None
    assert ptmx.writes == ["hí".encode("utf-8")]


def test_send_keys_error_returns_raw_exception():
    sess, _, _ = _session()

    class _Bad:
        def write(self, _):
            raise ValueError("nope")

    sess.ptmx = _Bad()
    err = sess.send_keys("x")
    assert isinstance(err, ValueError)


# ---------------------------------------------------------------------------
# check_and_handle_trust_prompt — provider-driven
# ---------------------------------------------------------------------------
def test_trust_prompt_dismisses_when_pattern_present():
    # Claude's trust pattern; dismissed with a carriage return.
    content = "Do you trust the files in this folder? (y/n)"
    sess, _, _ = _session(
        program="claude", output_func=lambda c: (content.encode(), None)
    )
    ptmx = _FakePtmx()
    sess.ptmx = ptmx
    assert sess.check_and_handle_trust_prompt() is True
    assert ptmx.writes == [b"\r"]


def test_trust_prompt_false_when_no_pattern():
    sess, _, _ = _session(
        program="claude", output_func=lambda c: (b"nothing interesting", None)
    )
    sess.ptmx = _FakePtmx()
    assert sess.check_and_handle_trust_prompt() is False


def test_trust_prompt_false_on_capture_error():
    sess, _, _ = _session(program="claude", output_func=lambda c: (b"", Exception("x")))
    assert sess.check_and_handle_trust_prompt() is False


# ---------------------------------------------------------------------------
# has_updated — hash change detection + idle prompt
# ---------------------------------------------------------------------------
def test_has_updated_detects_change_then_stable():
    outputs = iter(["first content", "first content"])
    sess, _, _ = _session(
        program="claude", output_func=lambda c: (next(outputs).encode(), None)
    )
    sess._monitor = tmux._new_status_monitor()
    # First call: prev hash is None -> changed.
    updated, _ = sess.has_updated()
    assert updated is True
    # Same content again -> not changed.
    updated2, _ = sess.has_updated()
    assert updated2 is False


def test_has_updated_reports_idle_prompt():
    idle = "No, and tell Claude what to do differently"
    sess, _, _ = _session(program="claude", output_func=lambda c: (idle.encode(), None))
    sess._monitor = tmux._new_status_monitor()
    _, has_prompt = sess.has_updated()
    assert has_prompt is True


def test_has_updated_false_false_on_capture_error():
    sess, _, _ = _session(program="claude", output_func=lambda c: (b"", Exception("x")))
    sess._monitor = tmux._new_status_monitor()
    assert sess.has_updated() == (False, False)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------
def test_restore_sets_ptmx_and_monitor():
    ptmx = _FakePtmx()
    sess, factory, _ = _session(pty_results=[(ptmx, None)])
    assert sess.restore() is None
    assert sess.ptmx is ptmx
    assert sess._monitor is not None
    # attach-session -t <name>
    assert factory.started_cmds[0].args == [
        "tmux",
        "attach-session",
        "-t",
        "mindflock_demo",
    ]


def test_restore_error_wrapped():
    sess, _, _ = _session(pty_results=[(None, Exception("pty fail"))])
    err = sess.restore()
    assert "error opening PTY: pty fail" in str(err)


# ---------------------------------------------------------------------------
# close — kill session + free ptmx
# ---------------------------------------------------------------------------
def test_close_kills_session_and_clears_ptmx():
    killed = {}

    def run_func(c):
        killed["args"] = c.args
        return None

    sess, _, _ = _session(run_func=run_func)
    ptmx = _FakePtmx()
    sess.ptmx = ptmx
    assert sess.close() is None
    assert ptmx.closed is True
    assert sess.ptmx is None
    assert killed["args"] == ["tmux", "kill-session", "-t", "mindflock_demo"]


def test_close_returns_kill_error():
    sess, _, _ = _session(run_func=lambda c: Exception("kill failed"))
    err = sess.close()
    assert "error killing tmux session: kill failed" in str(err)


def test_close_combines_ptmx_and_kill_errors():
    sess, _, _ = _session(run_func=lambda c: Exception("kill failed"))
    bad = _FakePtmx()
    bad.raise_on_close = True
    sess.ptmx = bad
    err = sess.close()
    msg = str(err)
    assert "multiple errors occurred during cleanup:" in msg
    assert "error closing PTY" in msg
    assert "error killing tmux session" in msg


# ---------------------------------------------------------------------------
# detach_safely
# ---------------------------------------------------------------------------
def test_detach_safely_noop_when_not_attached():
    sess, _, _ = _session()
    assert sess._attach_ch is None
    assert sess.detach_safely() is None


def test_detach_safely_closes_ptmx_and_clears_attach_state():
    import threading

    sess, _, _ = _session()
    ptmx = _FakePtmx()
    sess.ptmx = ptmx
    sess._attach_ch = threading.Event()
    sess._wg_active = False  # no threads to wait on
    sess._cancel = lambda: None
    assert sess.detach_safely() is None
    assert ptmx.closed is True
    assert sess.ptmx is None
    assert sess._attach_ch is None


def test_detach_safely_reports_ptmx_close_error():
    import threading

    sess, _, _ = _session()
    bad = _FakePtmx()
    bad.raise_on_close = True
    sess.ptmx = bad
    sess._attach_ch = threading.Event()
    sess._wg_active = False
    err = sess.detach_safely()
    assert "errors during detach" in str(err)
    assert "error closing attach pty session" in str(err)


# ---------------------------------------------------------------------------
# update_window_size — masking + field order
# ---------------------------------------------------------------------------
def test_update_window_size_forwards_masked_winsize(monkeypatch):
    seen = {}

    def fake_set_size(ptmx, ws):
        seen["ptmx"] = ptmx
        seen["rows"] = ws.rows
        seen["cols"] = ws.cols
        seen["x"] = ws.x
        seen["y"] = ws.y
        return None

    monkeypatch.setattr(tmux.pty_pkg, "set_size", fake_set_size)
    sess, _, _ = _session()
    ptmx = _FakePtmx()
    sess.ptmx = ptmx
    # Values above 0xFFFF are masked to 16 bits.
    assert sess.update_window_size(0x1_0050, 0x1_0030) is None
    assert seen["ptmx"] is ptmx
    assert seen["cols"] == 0x0050
    assert seen["rows"] == 0x0030
    assert seen["x"] == 0 and seen["y"] == 0


def test_set_detached_size_delegates_to_update(monkeypatch):
    calls = []
    sess, _, _ = _session()
    monkeypatch.setattr(
        sess, "update_window_size", lambda cols, rows: calls.append((cols, rows))
    )
    sess.set_detached_size(120, 40)
    assert calls == [(120, 40)]


# ---------------------------------------------------------------------------
# start — command building + failure paths
# ---------------------------------------------------------------------------
def test_start_returns_error_when_session_already_exists():
    # run() returning None => has-session succeeds => session exists.
    sess, _, _ = _session(run_func=lambda c: None)
    err = sess.start("/work/dir")
    assert "tmux session already exists: mindflock_demo" in str(err)


def test_start_env_prefix_and_launch_command(monkeypatch):
    # A session that does not exist yet, then exists after new-session.
    exist_state = {"created": False}

    def run_func(c):
        if c.args[:2] == ["tmux", "has-session"]:
            return None if exist_state["created"] else Exception("nope")
        # new-session is launched via the pty factory, not run(); set/history/
        # mouse all succeed.
        return None

    factory = _FakePtyFactory([(_FakePtmx(), None), (_FakePtmx(), None)])
    mock = cmd_pkg.MockCmdExec(run_func=run_func)
    sess = tmux.NewTmuxSessionWithDeps("demo", "claude", factory, mock)
    sess.launch_command = "claude --continue"
    sess.extra_env = {"PORT": "8080", "HOST": "x y"}

    # Flip existence to True right after new-session is dispatched to the pty.
    orig_start = factory.start

    def wrapped_start(cmd):
        exist_state["created"] = True
        return orig_start(cmd)

    factory.start = wrapped_start

    err = sess.start("/work/dir")
    assert err is None
    # The first pty command is the new-session; its final arg is the launch
    # string, prefixed with a shell-quoted env(1) block (sorted keys).
    new_session_cmd = factory.started_cmds[0]
    launch = new_session_cmd.args[-1]
    assert launch == "env HOST='x y' PORT=8080 claude --continue"
    assert new_session_cmd.args[:6] == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "mindflock_demo",
        "-c",
    ]


def test_start_pty_failure_returns_error(monkeypatch):
    # has-session always says "no session"; the pty factory fails to start.
    def run_func(c):
        return Exception("no session")

    factory = _FakePtyFactory([(None, Exception("pty start failed"))])
    mock = cmd_pkg.MockCmdExec(run_func=run_func)
    sess = tmux.NewTmuxSessionWithDeps("demo", "claude", factory, mock)
    err = sess.start("/work/dir")
    assert "error starting tmux session: pty start failed" in str(err)


# ---------------------------------------------------------------------------
# Constructors + free helpers
# ---------------------------------------------------------------------------
def test_new_tmux_session_sanitizes_name():
    sess = tmux.NewTmuxSession("My Session.1", "claude")
    assert sess.sanitized_name == "mindflock_MySession_1"
    assert sess.program == "claude"


def test_format_errs_matches_go_slice_render():
    errs = [Exception("a"), Exception("b")]
    assert tmux._format_errs(errs) == "[a b]"


def test_to_str_decodes_bytes_and_passes_str():
    assert tmux._to_str(b"ab") == "ab"
    assert tmux._to_str("cd") == "cd"


# ---------------------------------------------------------------------------
# WaitGroup / ctx helpers (Go sync.WaitGroup + context modelling)
# ---------------------------------------------------------------------------
def test_wait_group_add_done_wait_unblocks():
    sess, _, _ = _session()
    sess._wg_add(2)
    # A background thread that drains the group; _wg_wait must then return.
    import threading

    def _drain():
        sess._wg_done()
        sess._wg_done()

    t = threading.Thread(target=_drain)
    t.start()
    sess._wg_wait()  # returns once count hits 0
    t.join(timeout=1)
    assert sess._wg_count <= 0


def test_ctx_done_true_when_no_ctx_or_set():
    sess, _, _ = _session()
    # No ctx installed -> treated as done.
    assert sess._ctx_done() is True
    import threading

    sess._ctx = threading.Event()
    assert sess._ctx_done() is False
    sess._ctx.set()
    assert sess._ctx_done() is True


def test_ctx_wait_sleeps_when_no_ctx(monkeypatch):
    sess, _, _ = _session()
    slept = {}
    monkeypatch.setattr(tmux.time, "sleep", lambda s: slept.setdefault("s", s))
    assert sess._ctx_wait(0.01) is False
    assert slept["s"] == 0.01


def test_ctx_wait_returns_true_when_cancelled():
    import threading

    sess, _, _ = _session()
    sess._ctx = threading.Event()
    sess._ctx.set()
    assert sess._ctx_wait(0.01) is True


# ---------------------------------------------------------------------------
# start — restore failure + poll timeout + partial-session cleanup
# ---------------------------------------------------------------------------
def test_start_restore_failure_cleans_up(monkeypatch):
    exist_state = {"created": False}

    def run_func(c):
        if c.args[:2] == ["tmux", "has-session"]:
            return None if exist_state["created"] else Exception("nope")
        return None  # set-env / history / mouse / kill all succeed

    # First pty.start (new-session) OK; second (restore) fails.
    factory = _FakePtyFactory([(_FakePtmx(), None), (None, Exception("restore fail"))])
    mock = cmd_pkg.MockCmdExec(run_func=run_func)
    sess = tmux.NewTmuxSessionWithDeps("demo", "claude", factory, mock)

    orig = factory.start

    def wrapped(cmd):
        exist_state["created"] = True
        return orig(cmd)

    factory.start = wrapped
    err = sess.start("/wd")
    assert "error restoring tmux session: error opening PTY: restore fail" in str(err)


def test_start_times_out_waiting_for_session(monkeypatch):
    # Session never comes into existence; the poll loop must hit its deadline.
    def run_func(c):
        return Exception("never exists")  # has-session always fails

    factory = _FakePtyFactory([(_FakePtmx(), None)])
    mock = cmd_pkg.MockCmdExec(run_func=run_func)
    sess = tmux.NewTmuxSessionWithDeps("demo", "claude", factory, mock)

    # Jump the monotonic clock past the 2s deadline; no real sleeping.
    clock = {"t": 1000.0}
    monkeypatch.setattr(tmux.time, "monotonic", lambda: clock["t"])
    orig_sleep = tmux.time.sleep

    def fake_sleep(_s):
        clock["t"] += 10.0  # advance well past the deadline

    monkeypatch.setattr(tmux.time, "sleep", fake_sleep)
    err = sess.start("/wd")
    assert "timed out waiting for tmux session mindflock_demo" in str(err)
    _ = orig_sleep


def test_start_pty_failure_with_partial_session_runs_cleanup(monkeypatch):
    # has-session: False at the top guard, then True (a partial session got
    # created) so the cleanup kill-session path runs after the pty error.
    calls = {"has": 0, "killed": False}

    def run_func(c):
        if c.args[:2] == ["tmux", "has-session"]:
            calls["has"] += 1
            # 1st call (top guard): not exists. 2nd (post-failure): exists.
            return None if calls["has"] >= 2 else Exception("nope")
        if c.args[:2] == ["tmux", "kill-session"]:
            calls["killed"] = True
            return None
        return None

    factory = _FakePtyFactory([(None, Exception("pty boom"))])
    mock = cmd_pkg.MockCmdExec(run_func=run_func)
    sess = tmux.NewTmuxSessionWithDeps("demo", "claude", factory, mock)
    err = sess.start("/wd")
    assert "error starting tmux session: pty boom" in str(err)
    assert calls["killed"] is True


# ---------------------------------------------------------------------------
# cleanup_sessions — kill failure surfaces
# ---------------------------------------------------------------------------
def test_cleanup_sessions_kill_error_surfaces():
    listing = b"mindflock_a: 1 windows\nother: 1 windows\n"

    def output_func(c):
        return listing, None

    def run_func(c):
        return Exception("kill denied")

    mock = cmd_pkg.MockCmdExec(run_func=run_func, output_func=output_func)
    err = tmux.cleanup_sessions(mock)
    assert "failed to kill tmux session mindflock_a: kill denied" in str(err)


def test_cleanup_sessions_no_server_is_ok():
    # `tmux ls` exiting 1 (no server / no sessions) is not an error.
    def output_func(c):
        return b"", cmd_pkg.ExitError(1)

    mock = cmd_pkg.MockCmdExec(output_func=output_func)
    assert tmux.cleanup_sessions(mock) is None


def test_cleanup_sessions_list_error_surfaces():
    def output_func(c):
        return b"", Exception("tmux broken")

    mock = cmd_pkg.MockCmdExec(output_func=output_func)
    err = tmux.cleanup_sessions(mock)
    assert "failed to list tmux sessions: tmux broken" in str(err)
