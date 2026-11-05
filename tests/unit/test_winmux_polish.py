"""The session host (backend/session/winmux): the server's interpreter home
stays out of sessions, and ``stop_host`` ends every namespace — driven on
Linux through the same ptyprocess harness as test_winmux_host."""

from __future__ import annotations

import os
import sys
import time
import uuid

import pytest

pytest.importorskip("pyte")
pytest.importorskip("ptyprocess")

if os.name == "nt":  # pragma: no cover
    pytest.skip("the Linux harness drives ptyprocess", allow_module_level=True)

from backend.session.winmux import client, proto  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path))
    monkeypatch.setenv("WINMUX_IDLE_GRACE", "5")
    yield tmp_path
    info = client._read_info()
    for ns in ("default", "py", "other"):
        client.run(["tmux", "-L", ns, "kill-server"], capture_output=True)
    if info:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and client._pid_alive(info["pid"]):
            time.sleep(0.05)
        if client._pid_alive(info["pid"]):
            os.kill(info["pid"], 9)


def _wait(fn, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def _tmux(*args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 10)
    return client.run(["tmux", *args], **kw)


def test_server_python_home_never_reaches_a_session(home, tmp_path):
    # Start the host with a clean env, then open a namespace whose global
    # environment carries the server's interpreter home (as the server's own
    # env does under uv's Windows trampoline).
    r = _tmux("-L", "other", "new-session", "-d", "-s", "warm", "sleep 30")
    assert r.returncode == 0, r.stderr
    env = dict(os.environ)
    env.update(
        PYTHONHOME="/srv/py",
        UV_INTERNAL__PYTHONHOME="/srv/py",
        VIRTUAL_ENV=sys.prefix,
        KEEP_ME="1",
    )
    out = tmp_path / "env.txt"
    s = "e" + uuid.uuid4().hex[:6]
    cmd = (
        'printf "%%s|%%s|%%s|%%s|%%s\\n" "${PYTHONHOME-unset}" '
        '"${UV_INTERNAL__PYTHONHOME-unset}" "${VIRTUAL_ENV-unset}" "$KEEP_ME" '
        '"${EXPLICIT-unset}" > %s; sleep 30' % out
    )
    r = _tmux("-L", "py", "new-session", "-d", "-s", s, cmd, env=env)
    assert r.returncode == 0, r.stderr
    assert _wait(lambda: out.exists() and out.read_text().strip()) == (
        "unset|unset|unset|1|unset"
    )
    # An explicit -e still wins: only the INHERITED server home is dropped.
    out2 = tmp_path / "env2.txt"
    s2 = s + "x"
    r = _tmux(
        "-L",
        "py",
        "new-session",
        "-d",
        "-s",
        s2,
        "-e",
        "PYTHONHOME=/mine",
        'printf "%%s\\n" "$PYTHONHOME" > %s; sleep 30' % out2,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert _wait(lambda: out2.exists() and out2.read_text().strip()) == "/mine"


def test_stop_host_ends_every_namespace(home):
    assert not client.host_running()
    assert client.stop_host() is False  # nothing to stop
    for ns in ("default", "other"):
        r = _tmux("-L", ns, "new-session", "-d", "-s", "a" + ns, "sleep 60")
        assert r.returncode == 0, r.stderr
    info = client._read_info()
    assert info and client.host_running()
    assert client.stop_host(timeout=10) is True
    assert not client._pid_alive(int(info["pid"]))
    assert not os.path.exists(os.path.join(proto.run_dir(), "winmux.json"))
    # A later command starts a fresh host; the old sessions are gone.
    r = _tmux("-L", "other", "has-session", "-t", "aother")
    assert r.returncode == 1
