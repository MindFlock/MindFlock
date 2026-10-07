"""bridge.py: the in-sandbox TCP -> unix forwarder (runs standalone)."""

from __future__ import annotations

import ast
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parents[3] / "backend" / "peer" / "bridge.py"


def test_bridge_is_standalone_stdlib():
    tree = ast.parse(BRIDGE.read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            mods.add((node.module or "").split(".")[0])
    assert "backend" not in mods
    assert mods <= set(sys.stdlib_module_names), mods - set(sys.stdlib_module_names)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class UnixEcho:
    def __init__(self, path: str):
        self.path = path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(16)
        self.accepted = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._echo, args=(c,), daemon=True).start()

    @staticmethod
    def _echo(c):
        with c:
            while data := c.recv(65536):
                c.sendall(data)

    def close(self):
        self.sock.close()


@pytest.fixture
def run_bridge(tmp_path):
    started = []

    def start(sock_path, port):
        p = subprocess.Popen(
            [sys.executable, "-I", str(BRIDGE), sock_path, str(port)],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=tmp_path,
        )
        started.append(p)
        rc = p.wait(timeout=10)
        return p, rc

    yield start
    for p in started:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_bridge_splices_to_unix_socket(tmp_path, run_bridge):
    sock_path = str(tmp_path / "egress.sock")
    echo = UnixEcho(sock_path)
    port = _free_port()
    p, rc = run_bridge(sock_path, port)
    # Parent returns 0 only once the port is listening: connect immediately.
    assert rc == 0
    for i in range(3):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            payload = os.urandom(200_000)
            c.sendall(payload)
            c.shutdown(socket.SHUT_WR)
            got = b""
            while chunk := c.recv(65536):
                got += chunk
            assert got == payload
    assert echo.accepted == 3
    echo.close()


def test_bridge_only_listens_on_loopback(tmp_path, run_bridge):
    sock_path = str(tmp_path / "egress.sock")
    UnixEcho(sock_path)
    port = _free_port()
    _p, rc = run_bridge(sock_path, port)
    assert rc == 0
    out = subprocess.run(
        ["ss", "-ltnH", f"sport = :{port}"], capture_output=True, text=True
    )
    if out.returncode == 0 and out.stdout.strip():
        for line in out.stdout.strip().splitlines():
            assert line.split()[3] == f"127.0.0.1:{port}", line


def test_bridge_missing_upstream_closes_cleanly(tmp_path, run_bridge):
    port = _free_port()
    _p, rc = run_bridge(str(tmp_path / "absent.sock"), port)
    assert rc == 0
    with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
        c.settimeout(5)
        assert c.recv(10) == b""
    # Still serving after a failed upstream.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
        c.settimeout(5)
        assert c.recv(10) == b""


def test_bridge_usage_and_port_in_use(tmp_path, run_bridge):
    p, rc = run_bridge(str(tmp_path / "x.sock"), "notaport")
    assert rc == 64
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        p, rc = run_bridge(str(tmp_path / "x.sock"), busy.getsockname()[1])
        assert rc == 71
        assert b"cannot listen" in p.stderr.read()
    finally:
        busy.close()


def test_bridge_child_is_silent(tmp_path, run_bridge):
    """The serving child must not write to the agent's terminal."""
    sock_path = str(tmp_path / "egress.sock")
    port = _free_port()
    p, rc = run_bridge(sock_path, port)
    assert rc == 0
    for _ in range(3):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.settimeout(5)
            c.recv(1)
    time.sleep(0.2)
    os.killpg(p.pid, signal.SIGKILL)
    assert p.stdout.read() == b"" and p.stderr.read() == b""
