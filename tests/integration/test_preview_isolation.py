"""The UI preview, booted for real, proves its isolation.

One test, one real preview (``scripts/mfpreview``): a scratch server, the
``routes-failed`` scenario seeded through its API (two stub sessions in their
own tmux, a failed stop and a halt point), then a full teardown. Around it
the test fakes "the real machine" the preview must not touch:

* a fake real home (``$HOME``) with a ``.mindflock`` state + settings, a
  ``.mindflock-assistant`` dir, a ``.claude.json``, a ``.tmux.conf`` and a
  ``.gitconfig`` — byte-for-byte snapshotted before and compared after;
* a fake real tmux server: listening sockets at the caller's ``$TMUX`` and
  at ``$TMUX_TMPDIR/tmux-<uid>/default`` that count every connection — the
  preview must make none, though ``$TMUX`` is set as it is in any pane;
* leaky caller env (``CLAUDE_CONFIG_DIR``, ``GH_TOKEN``, ``PORT=9000``,
  ``CS_WEB_MODE=tailscale``, ``MINDFLOCK_SETTINGS_FILE`` at the fake home)
  that must not reach the server process.

Afterwards: no process tagged with the preview's id, no tmux socket, no
scratch dir, the port closed, the lock free. ~30 s; skipped without tmux/git
or with ``MINDFLOCK_SKIP_PREVIEW=1``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from mfpreview import sandbox, scenarios  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or shutil.which("tmux") is None
    or shutil.which("git") is None
    or os.environ.get("MINDFLOCK_SKIP_PREVIEW") == "1",
    reason="the preview isolation test needs Linux, tmux and git",
)


class CountingSocket:
    """A unix socket that accepts and counts connections (a fake tmux server)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.hits = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self._stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.hits += 1
            conn.close()

    def close(self) -> None:
        self._stop = True
        self.thread.join(timeout=2)
        self.sock.close()


def snapshot(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_socket():
                out[str(p)] = "socket"
                continue
            st = p.lstat()
            digest = (
                hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
            )
            out[str(p)] = (digest, st.st_mtime_ns, st.st_size)
    return out


def _environ(pid: int) -> dict:
    raw = Path("/proc/%d/environ" % pid).read_bytes().split(b"\0")
    return dict(
        item.decode("utf-8", "replace").split("=", 1) for item in raw if b"=" in item
    )


@pytest.fixture
def fake_machine(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="mfpv-it-", dir="/tmp"))
    home = base / "home"
    (home / ".mindflock").mkdir(parents=True)
    (home / ".mindflock" / "state.json").write_text(
        '{"instances": [{"title": "real-work"}]}'
    )
    (home / ".mindflock" / "settings.json").write_text(
        '{"general": {"onboarded": true}}'
    )
    (home / ".mindflock-assistant").mkdir()
    (home / ".mindflock-assistant" / "todos.json").write_text("[]")
    (home / ".claude.json").write_text('{"real": true}')
    (home / ".tmux.conf").write_text("set -g status on\n")
    (home / ".gitconfig").write_text("[user]\n\tname = Real Person\n")
    tmux_tmpdir = base / "tt"
    # tmux only talks to a socket dir that is private (0700), like the real one.
    (tmux_tmpdir / ("tmux-%d" % os.getuid())).mkdir(parents=True, mode=0o700)
    pane_sock = CountingSocket(tmux_tmpdir / "pane")
    default_sock = CountingSocket(tmux_tmpdir / ("tmux-%d" % os.getuid()) / "default")
    env = {
        "HOME": str(home),
        "TMUX": "%s,4242,0" % pane_sock.path,
        "TMUX_PANE": "%7",
        "TMUX_TMPDIR": str(tmux_tmpdir),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "GH_TOKEN": "ghp_must_not_leak",
        "PORT": "9000",
        "CS_WEB_MODE": "tailscale",
        "MINDFLOCK_HOME": str(home / ".mindflock"),
        "MINDFLOCK_SETTINGS_FILE": str(home / ".mindflock" / "settings.json"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MFPREVIEW_RUN_DIR", raising=False)
    try:
        yield {
            "base": base,
            "home": home,
            "socks": (pane_sock, default_sock),
            "env": env,
        }
    finally:
        pane_sock.close()
        default_sock.close()
        shutil.rmtree(base, ignore_errors=True)


def test_preview_never_touches_the_machine(fake_machine):
    home = fake_machine["home"]
    before = snapshot(home)
    run_dir = fake_machine["base"] / "run"
    scratch_parent = fake_machine["base"] / "scratch"
    scratch_parent.mkdir()
    sb = sandbox.Sandbox(
        scenario="routes-failed", scratch_parent=str(scratch_parent), run_dir=run_dir
    )
    pid_id = sb.id
    try:
        sb.start()
        assert sb.port not in sandbox.RESERVED_PORTS
        made = scenarios.seed(sb, "routes-failed")
        assert made["problems"] == []
        assert sorted(made["sessions"]) == ["flaky-ci", "search-index"]
        assert made["routes"]["failed"]["state"] == "halted"
        assert made["routes"]["halted"]["state"] in ("paused", "halted")

        # The sessions live on the preview's own tmux server …
        out = subprocess.run(
            [sb.real_tmux, "-S", str(sb.tmux_socket), "ls", "-F", "#S"],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )
        assert sorted(out.stdout.split()) == [
            "mindflock_flaky-ci",
            "mindflock_search-index",
        ]
        assert sandbox._inside(str(sb.tmux_socket), str(sb.scratch))

        # … and the server process never saw the caller's env.
        env = _environ(sb.proc.pid)
        assert env["HOME"] == str(sb.home)
        assert env["PORT"] == str(sb.port)
        assert env["CS_WEB_MODE"] == "local"
        assert env["MINDFLOCK_SETTINGS_FILE"] == str(sb.settings_path)
        for key in ("TMUX", "TMUX_PANE", "CLAUDE_CONFIG_DIR", "GH_TOKEN"):
            assert key not in env, key
        for v in env.values():
            assert str(home) not in v

        # Every process it started carries its tag (what the sweep kills).
        assert len(sandbox.preview_pids(pid_id)) >= 3
        assert sandbox.lock_held(run_dir)
        port = sb.port
    finally:
        report = sb.stop()

    assert report["scratch"] == "removed"
    assert not sb.scratch.exists()
    assert not sb.tmux_socket.exists()
    assert sandbox.preview_pids(pid_id) == []
    assert not sandbox.port_in_use(port)
    assert not sandbox.lock_held(run_dir)
    assert sandbox.read_state(run_dir) is None

    pane_sock, default_sock = fake_machine["socks"]
    assert pane_sock.hits == 0, "the preview contacted the caller's $TMUX server"
    assert default_sock.hits == 0, "the preview contacted the default tmux socket"
    assert snapshot(home) == before, "the preview changed the (fake) real home"
    assert json.loads((home / ".mindflock" / "state.json").read_text())[
        "instances"
    ] == [{"title": "real-work"}]
