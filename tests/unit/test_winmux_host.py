"""The session host end to end on Linux: a real daemon (started by the
client, as on Windows) with the ptyprocess backend, in a scratch
``MINDFLOCK_HOME``. Never touches tmux."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

import pytest

pytest.importorskip("pyte")
pytest.importorskip("ptyprocess")
if os.name == "nt":  # pragma: no cover
    pytest.skip("the Linux harness drives ptyprocess", allow_module_level=True)

from backend.session.winmux import client, proto  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _set_home(path):
    old = os.environ.get("MINDFLOCK_HOME")
    os.environ["MINDFLOCK_HOME"] = str(path)
    os.environ["WINMUX_IDLE_GRACE"] = "5"
    return old


def _restore_home(old):
    if old is None:
        os.environ.pop("MINDFLOCK_HOME", None)
    else:
        os.environ["MINDFLOCK_HOME"] = old


def _host_info():
    try:
        with open(os.path.join(proto.run_dir(), "winmux.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ns_list():
    return ["default", "other"]


@pytest.fixture(scope="module")
def home(tmp_path_factory):
    """One host for the module (sessions get unique names)."""
    path = tmp_path_factory.mktemp("winmux")
    old = _set_home(path)
    try:
        yield path
    finally:
        info = _host_info()
        for ns in ns_list():
            client.run(["tmux", "-L", ns, "kill-server"], capture_output=True)
        if info:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and client._pid_alive(info["pid"]):
                time.sleep(0.05)
            if client._pid_alive(info["pid"]):
                os.kill(info["pid"], 9)
        _restore_home(old)


@pytest.fixture
def fresh_home(tmp_path):
    """A host of its own (lifecycle tests)."""
    old = _set_home(tmp_path)
    try:
        yield tmp_path
    finally:
        info = _host_info()
        for ns in ns_list():
            client.run(["tmux", "-L", ns, "kill-server"], capture_output=True)
        if info:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and client._pid_alive(info["pid"]):
                time.sleep(0.05)
            if client._pid_alive(info["pid"]):
                os.kill(info["pid"], 9)
        _restore_home(old)


def tmux(*args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 10)
    return client.run(["tmux", *args], **kw)


def name():
    return "s" + uuid.uuid4().hex[:8]


def wait_for(fn, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError("timed out; last value: %r" % (last,))


def capture(s, *flags):
    return tmux("capture-pane", "-p", *flags, "-t", s).stdout


def new(s, cmd, *flags):
    r = tmux("new-session", "-d", "-s", s, *flags, cmd)
    assert r.returncode == 0, r.stderr
    return r


class Reader:
    """Collects an attached client's output on a thread."""

    def __init__(self, term):
        self.term = term
        self.buf = b""
        self.eof = False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while True:
            try:
                data = self.term.read(4096)
            except EOFError:
                self.eof = True
                return
            self.buf += data

    def has(self, needle: bytes):
        return needle in self.buf


# -- sessions, capture, keys --------------------------------------------------------


def test_new_session_and_capture(home):
    s = name()
    new(s, "printf 'hello\\n'; sleep 30")
    wait_for(lambda: "hello" in capture(s))
    out = capture(s)
    assert out.split("\n")[0] == "hello"
    assert out.count("\n") == 24  # every row of the default 80x24 pane, like tmux


def test_send_keys_literal_and_enter_into_cat(home):
    s = name()
    new(s, "cat")
    r = tmux(
        "send-keys",
        "-t",
        s,
        "-l",
        "--",
        "typed text;",
        ";",
        "send-keys",
        "-t",
        s,
        "Enter",
    )
    assert r.returncode == 0, r.stderr
    # tmux drops a trailing ';' of an argument (a separator): tty echo + cat.
    wait_for(lambda: capture(s).split("\n")[:2] == ["typed text", "typed text"])
    assert (
        tmux("send-keys", "-t", s, "-H", "61", "Enter").stderr == "invalid key: Enter\n"
    )
    tmux("send-keys", "-t", s, "-H", "61", "62", ";", "send-keys", "-t", s, "Enter")
    wait_for(lambda: capture(s).split("\n")[2:4] == ["ab", "ab"])
    tmux("send-keys", "-t", s, "-N", "3", "z", ";", "send-keys", "-t", s, "Enter")
    wait_for(lambda: "zzz" in capture(s))


def test_formats(home):
    s = name()
    new(s, "cat", "-x", "70", "-y", "11")
    t0 = time.time()
    fmt = (
        "#{session_name}|#{pane_current_command}|#{pane_width}x#{pane_height}"
        "|#{window_height}|#{||:#{pane_in_mode},#{mouse_any_flag}}|#{?pane_dead,dead,live}"
        "|#{pane_id}|#{history_size}|#{alternate_on}|#{scroll_position}|#{session_attached}"
    )
    out = wait_for(
        lambda: (lambda o: o if o.split("|")[1] == "cat" else None)(
            tmux("display-message", "-p", "-t", s, fmt).stdout.rstrip("\n")
        )
    )
    parts = out.split("|")
    assert parts[0] == s and parts[2] == "70x11" and parts[3] == "11"
    assert parts[4] == "0" and parts[5] == "live" and parts[6].startswith("%")
    assert parts[7] == "0" and parts[8] == "0" and parts[9] == "" and parts[10] == "0"
    created = int(tmux("display-message", "-p", "-t", s, "#{session_created}").stdout)
    assert abs(created - t0) < 5
    pid = int(tmux("display-message", "-p", "-t", s, "#{pane_pid}").stdout)
    os.kill(pid, 0)
    ls = tmux("list-sessions", "-F", "#{session_name} #{session_created}").stdout
    assert "%s %d" % (s, created) in ls.splitlines()
    lp = tmux("list-panes", "-a", "-F", "#{pane_id}\t#{session_name}").stdout
    assert "%s\t%s" % (parts[6], s) in lp.splitlines()
    # The agent_state batch: header display + capture per session, one call.
    r = tmux("display-message", "-p", "H0", ";", "capture-pane", "-p", "-t", s)
    assert r.stdout.startswith("H0\n") and r.stdout.count("\n") == 12


def test_has_session_exit_codes_and_targets(home):
    s = name()
    new(s, "sleep 30")
    assert tmux("has-session", "-t", s).returncode == 0
    assert tmux("has-session", "-t=" + s).returncode == 0
    assert tmux("has-session", "-t", "=" + s + ":").returncode == 0
    assert tmux("has-session", "-t", s + ":0.0").returncode == 0
    r = tmux("has-session", "-t", "nope-" + s)
    assert r.returncode == 1 and r.stderr == "can't find session: nope-%s\n" % s
    assert tmux("has-session", "-t", s[:-2]).returncode == 0  # unique prefix
    assert tmux("has-session", "-t", "=" + s[:-2]).returncode == 1  # exact only
    r = tmux("new-session", "-d", "-s", s, "true")
    assert r.returncode == 1 and r.stderr == "duplicate session: %s\n" % s
    r = tmux("frobnicate")
    assert r.returncode == 1 and r.stderr == "unknown command: frobnicate\n"


def test_chain_stops_at_first_failure(home):
    r = tmux(
        "display-message",
        "-p",
        "A",
        ";",
        "capture-pane",
        "-p",
        "-t",
        "missing-x",
        ";",
        "display-message",
        "-p",
        "B",
    )
    assert r.returncode == 1
    assert r.stdout == "A\n" and r.stderr == "can't find pane: missing-x\n"


def test_scrollback_and_history_limit(home):
    s = name()
    new(s, "seq 1 300; sleep 30", "-y", "10")
    wait_for(lambda: "300" in capture(s))
    out = capture(s, "-S", "-100")
    rows = out.split("\n")
    assert (
        "192" in rows and "300" in rows and "191" not in rows
    )  # 9 rows + the cursor row
    full = capture(s, "-S", "-", "-E", "-")
    assert full.startswith("1\n2\n")
    hs = int(tmux("display-message", "-p", "-t", s, "#{history_size}").stdout)
    assert hs >= 290
    tmux("set-option", "-t", s, "history-limit", "50")
    assert int(tmux("display-message", "-p", "-t", s, "#{history_size}").stdout) == 50
    assert tmux("show-options", "-v", "-t", s, "history-limit").stdout == "50\n"


def test_colours_and_joined_wraps(home):
    s = name()
    new(
        s, "printf '\\033[31mred\\033[0m\\n'; printf '%040d\\n' 7; sleep 30", "-x", "20"
    )
    wait_for(lambda: "red" in capture(s))
    assert capture(s, "-e").startswith("\x1b[31mred\x1b[0m\n")
    plain = capture(s).split("\n")
    assert plain[1] == "0" * 20 and plain[2] == "0" * 19 + "7"
    joined = capture(s, "-J").split("\n")
    assert joined[1] == "0" * 39 + "7"


def test_alternate_screen_and_mouse_flags(home):
    s = name()
    new(
        s,
        "printf 'main\\n'; sleep 0.3; printf '\\033[?1049h\\033[H\\033[?1000;1006hALT'; sleep 30",
    )
    wait_for(lambda: capture(s).startswith("ALT"))
    f = tmux(
        "display-message",
        "-p",
        "-t",
        s,
        "#{alternate_on}#{mouse_any_flag}#{mouse_sgr_flag}",
    )
    assert f.stdout == "111\n"
    assert tmux("capture-pane", "-p", "-a", "-t", s).stdout.startswith("main\n")


def test_buffers_and_bracketed_paste(home):
    s = name()
    new(s, "printf '\\033[?2004h'; cat -v")
    wait_for(
        lambda: tmux("display-message", "-p", "-t", s, "#{pane_pid}").returncode == 0
    )
    time.sleep(0.2)
    r = tmux("load-buffer", "-b", "seed", "-", input="one\ntwo")
    assert r.returncode == 0, r.stderr
    assert tmux("show-buffer", "-b", "seed").stdout == "one\ntwo"
    r = tmux("paste-buffer", "-b", "seed", "-p", "-r", "-d", "-t", s)
    assert r.returncode == 0, r.stderr
    tmux("send-keys", "-t", s, "Enter")
    wait_for(lambda: "^[[200~one" in capture(s))
    r = tmux("show-buffer", "-b", "seed")
    assert r.returncode == 1 and "seed" in r.stderr
    r = tmux("paste-buffer", "-b", "seed", "-t", s)
    assert r.returncode == 1 and r.stderr == "no buffer seed\n"


def test_load_buffer_from_file(home, tmp_path):
    p = tmp_path / "buf.txt"
    p.write_text("from file")
    s = name()
    new(s, "sleep 30")
    assert tmux("load-buffer", "-b", "f1", str(p)).returncode == 0
    assert tmux("show-buffer", "-b", "f1").stdout == "from file"
    assert tmux("delete-buffer", "-b", "f1").returncode == 0
    r = tmux("load-buffer", "-b", "f2", str(tmp_path / "nope"))
    assert r.returncode == 1 and "No such file" in r.stderr


def test_copy_mode_scroll_and_cancel(home):
    s = name()
    new(s, "seq 1 100; sleep 30", "-y", "10")
    wait_for(lambda: "100" in capture(s))
    assert tmux("send-keys", "-t", s, "-X", "cancel").stderr == "not in a mode\n"
    tmux("copy-mode", "-e", "-t", s)
    fmt = "#{pane_in_mode} #{scroll_position}"
    assert tmux("display-message", "-p", "-t", s, fmt).stdout == "1 0\n"
    tmux("send-keys", "-t", s, "-X", "-N", "5", "scroll-up")
    assert tmux("display-message", "-p", "-t", s, fmt).stdout == "1 5\n"
    tmux("send-keys", "-t", s, "-X", "-N", "5", "scroll-down")  # -e: bottom exits
    assert tmux("display-message", "-p", "-t", s, fmt).stdout == "0 \n"
    tmux("copy-mode", "-t", s)
    tmux("send-keys", "-t", s, "-X", "cancel")
    assert tmux("display-message", "-p", "-t", s, "#{pane_in_mode}").stdout == "0\n"


def test_environment_options_and_misc(home):
    s = name()
    new(s, "sleep 30", "-e", "FOO=bar")
    assert tmux("show-environment", "-t", s, "FOO").stdout == "FOO=bar\n"
    tmux("set-environment", "-t", s, "BAZ", "q")
    assert "BAZ=q" in tmux("show-environment", "-t", s).stdout.splitlines()
    r = tmux("show-environment", "-t", s, "NOPE")
    assert r.returncode == 1 and r.stderr == "unknown variable: NOPE\n"
    assert tmux("set-option", "-t", s, "mouse", "on").returncode == 0
    assert tmux("set-option", "-g", "some-unknown-option", "x").returncode == 0
    assert (
        tmux(
            "bind-key", "-T", "root", "WheelUpPane", "if-shell", "-F", "x", "y", "z"
        ).returncode
        == 0
    )
    assert tmux("resize-window", "-t", s, "-x", "50", "-y", "7").returncode == 0
    assert (
        tmux("display-message", "-p", "-t", s, "#{pane_width}x#{pane_height}").stdout
        == "50x7\n"
    )
    v = client.run(["tmux", "-V"], capture_output=True, text=True)
    assert v.stdout == "tmux 3.4 (winmux)\n" and v.returncode == 0


def test_child_env_and_in_session_tmux_shim(home, tmp_path):
    s = name()
    out = tmp_path / "shim.txt"
    new(
        s,
        'printf "%%s %%s\\n" "$TMUX" "$TMUX_PANE" > %s.env; '
        "tmux display-message -p -t \"$TMUX_PANE\" '#{session_name}' > %s; sleep 30"
        % (out, out),
        "-e",
        "MARK=1",
    )
    wait_for(lambda: out.exists() and out.read_text().strip(), timeout=10)
    assert out.read_text().strip() == s
    tmux_env, pane = (tmp_path / "shim.txt.env").read_text().split()
    assert tmux_env.startswith("winmux,") and pane.startswith("%")
    # $TMUX's first field as a socket is the default namespace (slot.py's read).
    r = tmux(
        "-S",
        tmux_env.split(",")[0],
        "display-message",
        "-p",
        "-t",
        pane,
        "#{session_name}",
    )
    assert r.stdout == s + "\n"


def test_rename_and_kill(home):
    s, s2 = name(), name()
    new(s, "sleep 30")
    pid = int(tmux("display-message", "-p", "-t", s, "#{pane_pid}").stdout)
    assert tmux("rename-session", "-t", s, s2).returncode == 0
    assert tmux("has-session", "-t", s).returncode == 1
    assert tmux("has-session", "-t", s2).returncode == 0
    assert tmux("kill-session", "-t", s2).returncode == 0
    assert tmux("has-session", "-t", s2).returncode == 1

    def gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open("/proc/%d/stat" % pid) as f:
                return f.read().split()[2] == "Z"
        except OSError:
            return True

    wait_for(gone)


def test_session_ends_with_its_program_unless_remain_on_exit(home):
    s, r = name(), name()
    new(s, "sleep 0.2")
    wait_for(lambda: tmux("has-session", "-t", s).returncode == 1)
    res = tmux(
        "new-session",
        "-d",
        "-s",
        r,
        "sleep 0.3",
        ";",
        "set-option",
        "-t",
        r,
        "remain-on-exit",
        "on",
    )
    assert res.returncode == 0, res.stderr
    wait_for(
        lambda: tmux("display-message", "-p", "-t", r, "#{pane_dead}").stdout == "1\n"
    )
    assert tmux("has-session", "-t", r).returncode == 0
    tmux("kill-session", "-t", r)


def test_popen_shape(home):
    s = name()
    new(s, "sleep 30")
    p = client.popen(
        ["tmux", "has-session", "-t", s], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    out, err = p.communicate(timeout=10)
    assert p.returncode == 0 and out == b"" and err == b""
    p = client.popen(
        ["tmux", "display-message", "-p", "-t", s, "#{session_name}"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert p.wait(timeout=10) == 0 and p.stdout.read() == s + "\n"
    with pytest.raises(subprocess.CalledProcessError):
        client.run(
            ["tmux", "has-session", "-t", "x-" + s], capture_output=True, check=True
        )


# -- attach -------------------------------------------------------------------------


def test_attach_repaint_live_bytes_and_resize(home):
    s = name()
    new(s, "printf 'before\\n'; cat", "-x", "40", "-y", "8")
    wait_for(lambda: "before" in capture(s))
    term = client.attach(s, dimensions=(10, 50))
    rd = Reader(term)
    wait_for(lambda: rd.has(b"before"))  # the repaint
    assert term.fd is None and term.isalive()
    # The window follows the (latest) client's size.
    wait_for(
        lambda: tmux(
            "display-message", "-p", "-t", s, "#{pane_width}x#{pane_height}"
        ).stdout
        == "50x10\n"
    )
    term.write(b"live\r")
    wait_for(lambda: rd.buf.count(b"live") >= 2)  # echo + cat
    term.setwinsize(12, 60)
    assert term.getwinsize() == (12, 60)
    wait_for(
        lambda: tmux(
            "display-message", "-p", "-t", s, "#{pane_width}x#{pane_height}"
        ).stdout
        == "60x12\n"
    )
    assert (
        tmux("list-clients", "-t", s, "-F", "#{client_pid}").stdout
        == "%d\n" % os.getpid()
    )
    term.close()
    wait_for(
        lambda: tmux("display-message", "-p", "-t", s, "#{session_attached}").stdout
        == "0\n"
    )
    assert tmux("has-session", "-t", s).returncode == 0  # detaching never kills


def test_two_clients_and_read_only(home):
    s = name()
    new(s, "cat")
    a = client.attach(s, dimensions=(24, 80))
    ro = client.attach(s, readonly=True, dimensions=(24, 80))
    ra, rr = Reader(a), Reader(ro)
    wait_for(
        lambda: tmux("display-message", "-p", "-t", s, "#{session_attached}").stdout
        == "2\n"
    )
    ro.write(b"ignored\r")
    a.write(b"shared\r")
    wait_for(lambda: rr.has(b"shared") and ra.has(b"shared"))
    time.sleep(0.2)
    assert "ignored" not in capture(s)
    lc = tmux("list-clients", "-t", s, "-F", "#{client_readonly}").stdout.split()
    assert sorted(lc) == ["0", "1"]
    tmux("detach-client", "-s", s)
    wait_for(lambda: ra.eof and rr.eof)
    assert b"[detached (from session %s)]" % s.encode() in ra.buf


def test_attach_to_missing_session_reads_the_error(home):
    term = client.attach("no-such-session-x")
    assert not term.isalive()
    assert term.read(1024) == b"can't find session: no-such-session-x\r\n"
    with pytest.raises(EOFError):
        term.read(10)


def test_client_detached_hook_kills_session(home):
    s = name()
    new(s, "sleep 30")
    assert (
        tmux(
            "set-hook", "-t", s, "client-detached", "kill-session -t %s" % s
        ).returncode
        == 0
    )
    term = client.attach(s)
    Reader(term)
    wait_for(
        lambda: tmux("display-message", "-p", "-t", s, "#{session_attached}").stdout
        == "1\n"
    )
    term.close()
    wait_for(lambda: tmux("has-session", "-t", s).returncode == 1)


def test_session_end_closes_clients(home):
    s = name()
    new(s, "cat")
    term = client.attach(s)
    rd = Reader(term)
    wait_for(
        lambda: tmux("display-message", "-p", "-t", s, "#{session_attached}").stdout
        == "1\n"
    )
    tmux("send-keys", "-t", s, "C-d")
    wait_for(lambda: rd.eof)
    assert rd.buf.endswith(b"[exited]\r\n")


# -- the host itself ---------------------------------------------------------------------


def test_no_server_is_not_started_by_queries(fresh_home):
    r = tmux("list-sessions")
    assert r.returncode == 1 and r.stderr.startswith("no server running on ")
    assert _host_info() is None
    assert tmux("kill-server").returncode == 1


def test_host_exits_after_last_session(fresh_home):
    s = name()
    new(s, "sleep 0.3")
    info = _host_info()
    assert info and client._pid_alive(info["pid"])
    wait_for(lambda: not client._pid_alive(info["pid"]), timeout=8)
    assert _host_info() is None
    assert tmux("has-session", "-t", s).stderr.startswith("no server running")


def test_token_required(home):
    new(name(), "sleep 30")
    info = _host_info()
    for token in ("", "wrong"):
        sock = socket.create_connection(("127.0.0.1", info["port"]), timeout=5)
        proto.send_json(sock, {"op": "run", "token": token, "argv": ["list-sessions"]})
        resp = json.loads(proto.recv_line(sock, bytearray()))
        sock.close()
        assert resp == {"ok": False, "error": "bad token"}


def test_namespaces_are_independent(home):
    s = name()
    assert tmux("-L", "other", "new-session", "-d", "-s", s, "sleep 30").returncode == 0
    assert tmux("has-session", "-t", s).returncode == 1
    assert tmux("-L", "other", "has-session", "-t", s).returncode == 0
    keep = name()
    new(keep, "sleep 30")
    assert tmux("-L", "other", "kill-server").returncode == 0
    assert tmux("-L", "other", "has-session", "-t", s).returncode == 1
    assert tmux("has-session", "-t", keep).returncode == 0


_CHILD = r"""
import sys
sys.path.insert(0, %r)
from backend.session.winmux import client
r = client.run(["tmux"] + sys.argv[1:], capture_output=True, text=True)
sys.stdout.write(r.stdout + "|" + str(r.returncode))
"""


def _child(*args):
    return subprocess.run(
        [sys.executable, "-c", _CHILD % ROOT, *args],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout


def test_persistence_across_client_processes_and_race_safe_start(fresh_home):
    a, b = name(), name()
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD % ROOT,
                "new-session",
                "-d",
                "-s",
                n,
                "sleep 30",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        for n in (a, b)
    ]
    outs = [p.communicate(timeout=30)[0] for p in procs]
    assert outs == ["|0", "|0"]
    listed = _child("list-sessions", "-F", "#{session_name}").split("|")[0].split()
    assert sorted(listed) == sorted([a, b])  # one host holds both
    assert _child("has-session", "-t", a) == "|0"


# -- sweep: defects pinned ---------------------------------------------------------------


def test_paste_buffer_reaches_a_pane_in_copy_mode(home):
    # A message to an agent rides load-buffer + paste-buffer -p -d; on Windows
    # it silently vanished (rc 0, buffer deleted) whenever the person had
    # scrolled the browser terminal into copy mode. tmux pastes in any mode.
    s = name()
    new(s, "seq 1 50; cat -v", "-y", "10")
    wait_for(lambda: "50" in capture(s))
    tmux("copy-mode", "-e", "-t", s)
    tmux("send-keys", "-t", s, "-X", "-N", "3", "scroll-up")
    assert tmux("load-buffer", "-b", "m", "-", input="hello-agent").returncode == 0
    r = tmux("paste-buffer", "-b", "m", "-p", "-r", "-d", "-t", s)
    assert r.returncode == 0, r.stderr
    tmux("send-keys", "-t", s, "-X", "cancel")
    tmux("send-keys", "-t", s, "Enter")
    wait_for(lambda: "hello-agent" in capture(s))


def test_attach_in_copy_mode_after_the_history_was_cleared(home):
    # Copy mode 10 lines up, then the history goes (clear-history, ESC[3J, a
    # lower history-limit): the attach repaint indexed lines that no longer
    # exist — IndexError, and the client stayed listed as attached for ever.
    s = name()
    new(s, "seq 1 60; cat", "-y", "8")
    wait_for(lambda: "60" in capture(s))
    tmux("copy-mode", "-t", s)
    tmux("send-keys", "-t", s, "-X", "-N", "10", "scroll-up")
    assert tmux("clear-history", "-t", s).returncode == 0
    term = client.attach(s, dimensions=(8, 80))
    rd = Reader(term)
    wait_for(lambda: rd.buf)  # the repaint arrived
    assert term.isalive()
    term.close()
    wait_for(
        lambda: tmux("display-message", "-p", "-t", s, "#{session_attached}").stdout
        == "0\n"
    )


def test_one_failed_connect_does_not_orphan_a_live_host(fresh_home, monkeypatch):
    # A single connect error to a host that is alive (a full accept backlog,
    # a busy machine) put it on the client's "stopped answering" list for the
    # rest of the process: every later query said "no server running", and
    # the next new-session started a SECOND host that overwrote the
    # rendezvous file, orphaning every agent in the first.
    s = name()
    new(s, "sleep 30")
    first = _host_info()
    real = client._connect
    fails = {"n": 1}

    def flaky(info, timeout):
        if fails["n"]:
            fails["n"] -= 1
            raise ConnectionRefusedError("backlog full")
        return real(info, timeout)

    monkeypatch.setattr(client, "_connect", flaky)
    try:
        assert tmux("has-session", "-t", s).returncode == 0
        assert tmux("has-session", "-t", s).returncode == 0
        s2 = name()
        new(s2, "sleep 30")
        assert _host_info()["pid"] == first["pid"]
        listed = tmux("list-sessions", "-F", "#{session_name}").stdout.split()
        assert sorted(listed) == sorted([s, s2])
    finally:
        monkeypatch.setattr(client, "_connect", real)
        client._bad.clear()
        tmux("kill-server")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and client._pid_alive(first["pid"]):
            time.sleep(0.05)
        if client._pid_alive(first["pid"]):
            os.kill(first["pid"], 9)
