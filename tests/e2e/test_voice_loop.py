"""The voice loop, server side, against a real sandboxed server (no browser).

flag → the installed hook script runs (Stop files the reply, UserPromptSubmit
prints the instruction) → ``reply.json`` → ``GET /voice`` → the idle event
with the spoken line on ``/api/events`` → the ``session_idle`` push body.

The agent is the journey tests' stub ``claude`` (``tests/e2e/_server``), which
never fires a hook, so the test runs the hook command the server installed in
the worktree's Claude settings file itself, exactly as Claude Code would:
``sh -c <command>`` with the event payload on stdin and the session name in
the environment (``MINDFLOCK_SESSION_NAME`` is the hook's first choice before
``tmux display-message``). Everything downstream of the hook — the reply
file, the routes, the state emitter, the ntfy dispatch — is the real server.

The emitter half pins the QA finding of 2026-10-23: a turn shorter than the
poll (the stub never reads "working") is still announced from the reply file,
and its ``turn_ended`` push carries the agent's words.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from backend.web.core import spoken

SAY = "All green. Should I push?"
REPLY = "Ran the tests; everything passes.\n\nSay: " + SAY


def _post(srv, path, body, timeout=30):
    req = urllib.request.Request(
        srv.base_url + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)


def _create_session(srv, title):
    status, _ = _post(
        srv, "/api/instances", {"title": title, "repo_path": str(srv.repo)}
    )
    assert status in (200, 202)
    deadline = time.time() + 30
    while time.time() < deadline:
        row = next((s for s in srv.state() if s.get("title") == title), None)
        if row and row.get("status") not in (None, "", "loading"):
            return row
        time.sleep(0.25)
    raise AssertionError("session %r never left loading: %r" % (title, srv.state()))


class _FakeNtfy:
    """Records every JSON publish the server POSTs."""

    def __init__(self):
        self.records = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 — quiet
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    outer.records.append(json.loads(raw.decode("utf-8")))
                except ValueError:
                    outer.records.append({"_raw": raw.decode("utf-8", "replace")})
                out = b'{"id":"fake","event":"message"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_PUT = do_POST

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _hook_commands(workdir):
    """``{event: [command, ...]}`` from the Claude settings file the server
    installed in ``workdir`` (the file name is composed, not spelled out)."""
    path = Path(workdir) / ".claude" / ("settings" + ".local.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for event, groups in (data.get("hooks") or {}).items():
        for group in groups or []:
            for hook in group.get("hooks") or []:
                cmd = hook.get("command")
                if isinstance(cmd, str) and cmd:
                    out.setdefault(event, []).append(cmd)
    return out


def _fire(srv, command, payload, session_name):
    """Run one installed hook command the way the CLI would."""
    env = {
        **os.environ,
        "HOME": str(srv.home),
        "MINDFLOCK_SESSION_NAME": session_name,
        "PATH": srv.env["PATH"],
    }
    env.pop("MINDFLOCK_ACTIVITY_MARKER_DIR", None)
    env.pop("MINDFLOCK_VOICE_FLAG_DIR", None)
    env.pop("MINDFLOCK_THREAD_MARKER_DIR", None)
    env.pop("TMUX_PANE", None)
    cp = subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(payload).encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=30,
        cwd=str(srv.repo),
    )
    return cp.returncode, cp.stdout.decode("utf-8", "replace")


def _events(srv, want, timeout, seen=None):
    """Frames from ``/api/events`` until ``want(frame)`` is true (returned) or
    ``timeout`` seconds pass (None). Every frame read lands in ``seen``."""
    websockets = pytest.importorskip("websockets")
    from websockets.sync.client import connect

    url = srv.base_url.replace("http://", "ws://") + "/api/events"
    deadline = time.time() + timeout
    with connect(url, open_timeout=10) as ws:
        while time.time() < deadline:
            try:
                raw = ws.recv(timeout=max(0.5, deadline - time.time()))
            except TimeoutError:
                break
            except websockets.exceptions.ConnectionClosed:
                break
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if seen is not None:
                seen.append(frame)
            if want(frame):
                return frame
    return None


def test_voice_loop_from_flag_to_push(mf_server):
    started = time.time()
    srv = mf_server
    ntfy = _FakeNtfy()
    try:
        _post(
            srv,
            "/api/settings",
            {
                "notifications": {
                    "ntfy_enabled": True,
                    "ntfy_server": ntfy.url,
                    "ntfy_topic": "voiceqa",
                    "ntfy_agent_words": True,
                }
            },
        )
        row = _create_session(srv, "talk")
        name = row.get("tmux_name") or "mindflock_talk"
        workdir = row.get("folder") or row.get("path")
        assert workdir and os.path.isdir(workdir), row

        # --- the flag ------------------------------------------------------
        status, body = _post(srv, "/api/instances/talk/voice", {"on": True})
        assert status == 200 and body["on"] is True and body["say"] is None, body
        flag = srv.home / ".mindflock-assistant" / ".voice" / (name + ".on")
        assert flag.is_file()
        rows = {s["title"]: s for s in srv.state()}
        assert rows["talk"]["voice"] is True

        # --- the installed hook script -------------------------------------
        hooks = _hook_commands(workdir)
        assert "Stop" in hooks and "UserPromptSubmit" in hooks, sorted(hooks)
        stop_cmd = next(c for c in hooks["Stop"] if "reply.json" in c)
        prompt_cmd = next(
            c for c in hooks["UserPromptSubmit"] if "hook_event_name" in c
        )
        submit = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sid-1",
            "prompt": "hi",
        }
        # The sticky flag asks nothing of the session by itself: the Say line
        # is requested per prompt (`/send {say: true}` arms the one-shot .next
        # the hook consumes once, printing only while it is fresh).
        rc, out = _fire(srv, prompt_cmd, submit, name)
        assert rc == 0 and spoken.INSTRUCTION not in out, (rc, out)
        nxt = flag.with_name(flag.name[: -len(".on")] + ".next")
        nxt.write_text(str(int(time.time())), encoding="utf-8")
        rc, out = _fire(srv, prompt_cmd, submit, name)
        assert rc == 0 and spoken.INSTRUCTION in out and not nxt.exists(), (rc, out)
        rc, out = _fire(srv, prompt_cmd, submit, name)
        assert rc == 0 and spoken.INSTRUCTION not in out, (rc, out)
        # Off: the same hook prints nothing.
        _post(srv, "/api/instances/talk/voice", {"on": False})
        rc, out = _fire(
            srv,
            prompt_cmd,
            {"hook_event_name": "UserPromptSubmit", "session_id": "sid-1"},
            name,
        )
        assert rc == 0 and spoken.INSTRUCTION not in out, (rc, out)
        _post(srv, "/api/instances/talk/voice", {"on": True})

        # The server's boot-quiet window (state_events._BOOT_QUIET_SECONDS, 30 s
        # after launch) swallows every transition it sees — a restart
        # rediscovering parked sessions is not news — and the prompt hook
        # above wrote a "working" marker, so a Stop inside that window would
        # be a transition the quiet window ate rather than one the poll
        # missed. Wait it out; the sandbox booted moments before this test.
        while time.time() - started < 36:
            time.sleep(0.5)
        rc, out = _fire(
            srv,
            stop_cmd,
            {
                "hook_event_name": "Stop",
                "session_id": "sid-1",
                "last_assistant_message": REPLY,
            },
            name,
        )
        assert rc == 0, (rc, out)
        reply = (
            srv.home
            / ".mindflock-assistant"
            / ".activity-markers"
            / (name + ".reply.json")
        )
        assert reply.is_file()
        assert json.loads(reply.read_text())["text"] == REPLY

        # --- the routes --------------------------------------------------------
        got = srv.api("/api/instances/talk/voice")
        assert (
            got["say"] == SAY
            and got["source"] == "hook"
            and got["has_question"] is True
        )
        assert got["on"] is True and got["ts"]
        view = srv.api("/api/instances/talk/output?view=say")
        assert (
            view["view"] == "say"
            and view["text"] == SAY
            and view["has_question"] is True
        )
        # The row field is memoized per snapshot (~2.5 s): give it a moment.
        deadline = time.time() + 15
        last_say = None
        while time.time() < deadline and last_say != SAY:
            rows = {s["title"]: s for s in srv.state()}
            last_say = rows["talk"].get("last_say")
            if last_say != SAY:
                time.sleep(0.5)
        assert last_say == SAY, rows["talk"]

        # --- the event (a turn shorter than the poll) ----------------------------
        # Announced once the server's 30 s boot-quiet window is over (the
        # sandbox booted seconds ago), so allow a comfortable wait.
        seen = []
        frame = _events(
            srv,
            lambda f: f.get("event") == "session.activity_changed"
            and f.get("session") == "talk"
            and f.get("new") == "idle"
            and (f.get("data") or {}).get("say"),
            timeout=75,
            seen=seen,
        )
        assert frame is not None, "no idle event with the spoken line; frames: %r" % (
            [
                (f.get("seq"), f.get("event"), f.get("session"), f.get("new"))
                for f in seen
            ],
        )
        assert frame["data"] == {"say": SAY, "has_question": True, "source": "hook"}

        # --- the push --------------------------------------------------------------
        deadline = time.time() + 90
        pushed = None
        while time.time() < deadline and pushed is None:
            pushed = next(
                (
                    r
                    for r in ntfy.records
                    if str(r.get("title", "")).startswith("talk has finished")
                ),
                None,
            )
            time.sleep(0.5)
        assert pushed is not None, ntfy.records
        # Agent words on: the body IS the spoken line (no URL appended — there
        # is no phone URL in a tailnet-less sandbox, and a read-aloud body
        # never carries one anyway).
        assert pushed["message"] == SAY, pushed
        assert pushed.get("topic") == "voiceqa"
    finally:
        ntfy.stop()
