"""Two sandboxed MindFlock servers that can join each other over a peer link.

Built on :class:`tests.e2e._server.SandboxServer` (scratch ``$HOME``, its own
tmux server, ``CS_CURSOR_AUTOADOPT=0``, a throwaway repo), plus what a peer
link needs to stay on this machine:

* ``peer`` settings with ``relay = "off"`` and ``advertise_host 127.0.0.1``,
  so an invite names this machine's loopback listener on a free port;
* a ``tailscale`` and ``cloudflared`` on ``PATH`` that always fail, the
  tailscale status hook pointed at nothing and ``MINDFLOCK_CLOUDFLARED`` at a
  missing file, so neither the owner's tailnet nor a real quick tunnel is ever
  reached (a guest link then answers only its ``local_url``);
* ``MINDFLOCK_PEER_HOME`` under the short runtime dir (agent-API sockets live
  there and a socket path is capped at 108 bytes);
* optionally the access gate ON with a fixed owner token (guest links and the
  join page need it): :attr:`token`, sent as a bearer header by :meth:`call`.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from tests.e2e import _server


class PeerServer(_server.SandboxServer):
    def __init__(self, scratch: Path, name: str, token: str = "") -> None:
        super().__init__(scratch, onboarded=True, stub_agent=True)
        self.name = name
        self.token = token
        self.peer_port = _server.free_port()

    # -- layout --------------------------------------------------------------
    def _lay_out_scratch(self) -> None:
        super()._lay_out_scratch()
        self.settings_path.write_text(
            json.dumps(
                {
                    "general": {"onboarded": True},
                    "peer": {
                        "enabled": True,
                        "relay": "off",
                        "advertise_host": "127.0.0.1",
                        "listen_host": "127.0.0.1",
                        "listen_port": self.peer_port,
                        "display_name": self.name,
                    },
                }
            )
        )

    def _build_env(self) -> None:
        super()._build_env()
        bin_dir = self.scratch / "bin"
        for exe in ("tailscale", "cloudflared"):
            stub = bin_dir / exe
            stub.write_text("#!/bin/sh\necho 'not in the join trial' >&2\nexit 1\n")
            stub.chmod(0o755)
        tmp = self.scratch / "tmp"
        tmp.mkdir(exist_ok=True)
        self.env.update(
            {
                "TMPDIR": str(tmp),
                "MINDFLOCK_TAILSCALE_STATUS_FILE": str(
                    self.scratch / "no-tailnet.json"
                ),
                "MINDFLOCK_TAILSCALE_BIN": str(bin_dir / "tailscale"),
                "MINDFLOCK_CLOUDFLARED": str(self.scratch / "no-cloudflared"),
                "MINDFLOCK_PEER_HOME": str(self.tmux_tmpdir / "peer"),
            }
        )
        if self.token:
            self.env["MINDFLOCK_AUTH"] = "1"
            self.env["MINDFLOCK_AUTH_TOKEN"] = self.token

    # -- a crash and a restart (the tmux server and every file stay) -------------
    def kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)

    def relaunch(self, timeout: float = 60.0) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "backend.web.run", "local"],
            cwd=str(self.repo),
            env=self.env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        wait_until(
            lambda: self.call("GET", "/api/remote/hello")[0] == 200, timeout, "restart"
        )

    # -- calls ---------------------------------------------------------------
    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        token: Optional[str] = None,
        timeout: float = 20.0,
    ) -> Tuple[int, Any]:
        """``(status, json-or-text)`` — never raises on an HTTP error."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        tok = self.token if token is None else token
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except (urllib.error.URLError, OSError) as e:  # not up (yet)
            return 0, str(e)
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, raw.decode(errors="replace")

    def ok(self, method: str, path: str, body: Any = None, **kw) -> Any:
        status, data = self.call(method, path, body, **kw)
        if status >= 300:
            raise AssertionError(
                "%s %s %s → %s %r" % (self.name, method, path, status, data)
            )
        return data

    def ws_url(self, path: str) -> str:
        return "ws://127.0.0.1:%d%s" % (self.port, path)

    def ws_headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer " + self.token} if self.token else {}

    # -- tmux (this server's own socket, every call) ---------------------------
    def tmux(self, *args: str) -> str:
        out = subprocess.run(
            ["tmux", "-S", str(self.tmux_socket), *args],
            env={**self.env, "TMUX": ""},
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout

    def tmux_name(self, title: str) -> str:
        """The tmux session name the engine gave ``title``."""
        for line in self.tmux("list-sessions", "-F", "#{session_name}").splitlines():
            if line.endswith(title) or line.endswith(title.replace("-", "_")):
                return line
        raise AssertionError("no tmux session for %s: %s" % (title, self.tmux("ls")))

    def pane(self, title: str) -> str:
        return self.tmux("capture-pane", "-p", "-J", "-t", self.tmux_name(title))

    # -- sessions ---------------------------------------------------------------
    def new_bash(self, title: str, timeout: float = 30.0) -> dict:
        self.ok(
            "POST",
            "/api/instances",
            {"title": title, "program": "bash", "repo_path": str(self.repo)},
        )
        return self.wait_row(
            title, lambda r: r.get("status") not in ("loading", ""), timeout
        )

    def row(self, title: str) -> Optional[dict]:
        rows = self.ok("GET", "/api/instances")
        rows = rows.get("instances", rows) if isinstance(rows, dict) else rows
        for r in rows:
            if r.get("title") == title:
                return r
        return None

    def wait_row(self, title: str, pred=lambda r: True, timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.row(title)
            if last is not None and pred(last):
                return last
            time.sleep(0.3)
        raise AssertionError(
            "%s: row %s never matched (last %r)" % (self.name, title, last)
        )


def wait_until(pred, timeout: float = 15.0, what: str = "condition", step: float = 0.3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(step)
    raise AssertionError("timed out waiting for %s (last %r)" % (what, last))
