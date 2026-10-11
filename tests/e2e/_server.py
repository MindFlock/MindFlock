"""A sandboxed MindFlock server for the journey tests.

Boots ``python -m backend.web.run local`` (the same entry ``mindflock serve``
uses) the way the cold-install CI job does: a stub ``claude`` on ``PATH`` that
announces itself and idles, so a session starts without any credentials.

Everything the server could touch is redirected into a scratch directory:

* ``$HOME`` (so ``~/.mindflock`` — state, settings, recently-closed list,
  worktrees — and ``~/.claude.json`` are scratch files), with the settings
  store pointed at an explicit file so a journey can start from
  ``onboarded: false``;
* its own tmux server (``$TMUX_TMPDIR`` under a short runtime dir, ``$TMUX``
  unset so it cannot reach the developer's tmux);
* ``CS_CURSOR_AUTOADOPT=0`` (a sandboxed server otherwise adopts the
  developer's real Cursor windows), ``MINDFLOCK_AUTH=0`` (no token gate),
  ``CS_WEB_MODE`` unset (``run.py local`` sets it itself), a free port;
* a throwaway git repository as the working directory, with its own git
  identity.

Teardown kills the server by pid and the scratch tmux server by socket.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

STUB_AGENT = """#!/bin/sh
# Stub coding agent for the journey tests: accepts any args, announces itself,
# then idles so the tmux session stays alive like a real agent waiting for input.
echo "[stub-claude] started with args: $*"
while true; do sleep 60; done
"""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_json(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _wait_http(url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def _git(repo: Path, env: Dict[str, str], *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class SandboxServer:
    """One booted server. Use :func:`boot`; call :meth:`stop` when done."""

    def __init__(self, scratch: Path, onboarded: bool, stub_agent: bool = True) -> None:
        self.scratch = scratch
        self.onboarded = onboarded
        # False = a machine with no agent CLI at all (the first-run story):
        # no stub on PATH, no login file, and every PATH entry that holds a
        # real ``claude`` is dropped so the developer's own install stays
        # invisible to the sandbox.
        self.stub_agent = stub_agent
        self.port = free_port()
        self.base_url = "http://127.0.0.1:%d" % self.port
        self.home = scratch / "home"
        self.settings_path = self.home / ".mindflock" / "settings.json"
        self.repo = scratch / "repo"
        self.log_path = scratch / "server.log"
        # tmux refuses long socket paths; keep the socket under a short runtime
        # dir rather than pytest's deep tmp tree.
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        self.tmux_tmpdir = Path(tempfile.mkdtemp(prefix="mfe2e-", dir=runtime))
        self.tmux_socket = self.tmux_tmpdir / ("tmux-%d" % os.getuid()) / "default"
        self.proc: Optional[subprocess.Popen] = None
        self.env: Dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = 60.0) -> None:
        self._lay_out_scratch()
        self._build_env()
        self.log = open(self.log_path, "w")  # noqa: SIM115 — lives with the process
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "backend.web.run", "local"],
            cwd=str(self.repo),
            env=self.env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if not _wait_http(self.base_url + "/api/remote/hello", timeout):
            self.stop()
            raise RuntimeError(
                "sandbox server did not answer /api/remote/hello within %ss:\n%s"
                % (timeout, self.log_text()[-4000:])
            )

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.wait(timeout=5)
        # The scratch tmux server, by its own socket only — never the default.
        if self.tmux_socket.exists():
            subprocess.run(
                ["tmux", "-S", str(self.tmux_socket), "kill-server"],
                env={**self.env, "TMUX": ""},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            deadline = time.time() + 3
            while self.tmux_socket.exists() and time.time() < deadline:
                time.sleep(0.1)
        try:
            self.log.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmux_tmpdir, ignore_errors=True)

    # -- helpers for tests ---------------------------------------------------
    def api(self, path: str) -> Any:
        return http_json(self.base_url + path)

    def state(self) -> Any:
        """The engine's view of its sessions (``GET /api/instances``)."""
        return self.api("/api/instances")

    def settings(self) -> Dict[str, Any]:
        try:
            return json.loads(self.settings_path.read_text())
        except (OSError, ValueError):
            return {}

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    # -- internals -----------------------------------------------------------
    def _lay_out_scratch(self) -> None:
        (self.home / ".mindflock").mkdir(parents=True)
        self.settings_path.write_text(
            json.dumps({"general": {"onboarded": bool(self.onboarded)}})
        )
        # Minimal login evidence so the agent's auth probe passes (what the
        # cold-install job writes too).
        if self.stub_agent:
            (self.home / ".claude.json").write_text(
                '{"oauthAccount": {"stub": true}}\n'
            )
        bin_dir = self.scratch / "bin"
        bin_dir.mkdir()
        if self.stub_agent:
            stub = bin_dir / "claude"
            stub.write_text(STUB_AGENT)
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        gitconfig = self.scratch / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = journey\n\temail = journey@example.com\n"
            "[init]\n\tdefaultBranch = main\n"
        )
        self._gitconfig = gitconfig
        self.repo.mkdir()
        git_env = {**os.environ, "GIT_CONFIG_GLOBAL": str(gitconfig)}
        _git(self.repo, git_env, "init", "-q")
        (self.repo / "README.md").write_text("hello\n")
        _git(self.repo, git_env, "add", "README.md")
        _git(self.repo, git_env, "commit", "-qm", "initial")

    def _build_env(self) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if not (
                k.startswith("MINDFLOCK_")
                or k.startswith("CS_")
                or k in ("TMUX", "TMUX_PANE", "PORT", "UVICORN_PORT")
            )
        }
        path_dirs = [d for d in env.get("PATH", "").split(os.pathsep) if d]
        if not self.stub_agent:
            path_dirs = [
                d
                for d in path_dirs
                if not os.access(os.path.join(d, "claude"), os.X_OK)
            ]
        env.update(
            {
                "HOME": str(self.home),
                "PATH": os.pathsep.join([str(self.scratch / "bin"), *path_dirs]),
                "PORT": str(self.port),
                "MINDFLOCK_AUTH": "0",
                "MINDFLOCK_SETTINGS_FILE": str(self.settings_path),
                # The pipeline ledger root: never the owner's checkout.
                "MINDFLOCK_REPO_ROOT": str(self.repo),
                "MINDFLOCK_EXTENSIONS_DIR": str(self.scratch / "extensions"),
                "MINDFLOCK_HOOKS_DIR": str(self.scratch / "no-hooks"),
                "CS_CURSOR_AUTOADOPT": "0",
                "MINDFLOCK_REVIVE_ON_START": "0",
                "TMUX_TMPDIR": str(self.tmux_tmpdir),
                "GIT_CONFIG_GLOBAL": str(self._gitconfig),
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONPATH": str(REPO_ROOT),
                "PYTHONUNBUFFERED": "1",
            }
        )
        (self.scratch / "extensions").mkdir(exist_ok=True)
        self.env = env


def boot(
    scratch: Path,
    onboarded: bool = True,
    stub_agent: bool = True,
    timeout: float = 60.0,
) -> SandboxServer:
    srv = SandboxServer(scratch, onboarded=onboarded, stub_agent=stub_agent)
    srv.start(timeout=timeout)
    return srv
