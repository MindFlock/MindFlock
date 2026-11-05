"""The session host daemon — tmux's server, for native Windows.

One per user, started on demand by the first client that needs it
(``new-session`` / ``start-server``, as tmux starts its server), detached
from every console and job so it outlives the MindFlock server like a tmux
server does, and gone once its last session ends (tmux's ``exit-empty``).

It listens on ``127.0.0.1:<random port>`` and publishes ``{port, pid,
token, proto}`` in ``<MINDFLOCK_HOME or ~/.mindflock>/run/winmux.json``;
every request must carry the token (the port is reachable by every local
user, the profile folder is not).

State lives under one lock. Nothing that can block on a child happens
under it: pane input goes through a per-pane writer thread, output to a
client through a per-client writer thread, and a kill ends the child in
the background — so a wedged agent can never wedge the host.
"""

from __future__ import annotations

import collections
import hmac
import json
import os
import queue
import secrets
import socket
import socketserver
import struct
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from backend import compat
from backend.session.winmux import formats, ptys, proto
from backend.session.winmux.proto import CmdError
from backend.session.winmux.screen import DEFAULT_HISTORY, Term

__all__ = ["Host", "main"]

#: tmux's default ``update-environment``: what a new-session client's own
#: environment overrides in the global one.
UPDATE_ENVIRONMENT = (
    "DISPLAY KRB5CCNAME MSYSTEM SSH_ASKPASS SSH_AUTH_SOCK SSH_AGENT_PID "
    "SSH_CONNECTION WINDOWID XAUTHORITY"
)

#: Options with their tmux defaults, as ``show-options`` reports them.
OPTION_DEFAULTS: Dict[str, str] = {
    "history-limit": str(DEFAULT_HISTORY),
    "mouse": "off",
    "status": "on",
    "remain-on-exit": "off",
    "exit-empty": "on",
    "window-size": "latest",
    "default-terminal": "xterm-256color",
    "default-shell": "",
    "base-index": "0",
    "escape-time": "500",
    "set-clipboard": "external",
    "allow-passthrough": "off",
    "focus-events": "off",
    "aggressive-resize": "off",
    "alternate-screen": "on",
    "default-size": "80x24",
    "buffer-limit": "50",
    "update-environment": UPDATE_ENVIRONMENT,
    "prefix": "C-b",
    "destroy-unattached": "off",
    "detach-on-destroy": "on",
}

BOOL_OPTIONS = {
    "mouse",
    "status",
    "remain-on-exit",
    "exit-empty",
    "focus-events",
    "aggressive-resize",
    "alternate-screen",
    "allow-passthrough",
    "destroy-unattached",
}

#: Lines of history a newly attached client is sent ahead of the screen, so
#: its own scrollback holds them (the browser scrolls that natively).
REPAINT_HISTORY = 1000

#: A client this far behind is dropped rather than buffered without end.
CLIENT_QUEUE_LIMIT = 32 << 20


def _now() -> float:
    return time.time()


class Ctx:
    """One request's context: who asked, and what it printed."""

    def __init__(
        self,
        host: "Host",
        ns: "Namespace",
        cwd: str = "",
        env: Optional[Dict[str, str]] = None,
        stdin: bytes = b"",
        pid: int = 0,
        client: Optional["Client"] = None,
    ) -> None:
        self.host = host
        self.ns = ns
        self.cwd = cwd
        self.env = env or {}
        self.stdin = stdin
        self.pid = pid
        self.client = client
        self.out: List[bytes] = []

    def print(self, text: str) -> None:
        self.out.append(text.encode("utf-8", "surrogateescape") + b"\n")

    def write(self, data: bytes) -> None:
        self.out.append(data)


class Namespace:
    """One tmux server's worth of state (``-L``/``-S``; see proto)."""

    def __init__(self, name: str, env: Dict[str, str]) -> None:
        self.name = name
        self.sessions: Dict[str, "Session"] = {}
        self.options: Dict[str, str] = {}
        self.env: Dict[str, Optional[str]] = {
            k: v for k, v in env.items() if k not in ("TMUX", "TMUX_PANE")
        }
        self.hooks: Dict[str, List[str]] = {}
        self.buffers: "collections.OrderedDict[str, Buffer]" = collections.OrderedDict()
        self.bindings: Dict[Tuple[str, str], List[str]] = {}
        self.buffer_seq = 0
        self.created = _now()

    def pane_by_id(self, pid_s: str) -> Optional["Pane"]:
        for s in self.sessions.values():
            if s.pane and "%%%d" % s.pane.id == pid_s:
                return s.pane
        return None


class Buffer:
    def __init__(self, name: str, data: bytes, auto: bool) -> None:
        self.name = name
        self.data = data
        self.auto = auto
        self.created = _now()


class Session:
    def __init__(self, ns: Namespace, name: str, sid: int, wid: int) -> None:
        self.ns = ns
        self.name = name
        self.id = sid
        self.window_id = wid
        self.window_name = ""
        self.created = _now()
        self.activity = self.created
        self.last_attached = 0.0
        self.env: Dict[str, Optional[str]] = {}
        self.options: Dict[str, str] = {}
        self.hooks: Dict[str, List[str]] = {}
        self.pane: Optional["Pane"] = None
        self.clients: List["Client"] = []
        self.manual_size: Optional[Tuple[int, int]] = None


class CopyMode:
    """tmux's copy mode, as far as MindFlock drives it: a frozen view
    ``offset`` lines up from the bottom (``#{scroll_position}``)."""

    def __init__(self, exit_on_bottom: bool) -> None:
        self.offset = 0
        self.exit_on_bottom = exit_on_bottom


class Pane:
    def __init__(self, pid_n: int, session: Session, pty: ptys.Pty, term: Term):
        self.id = pid_n
        self.session = session
        self.pty = pty
        self.term = term
        self.start_cmd = ""
        self.start_path = ""
        self.created = _now()
        self.dead = False
        self.dead_status: Optional[int] = None
        self.gone = False  # destroyed: output is drained and dropped
        self.mode: Optional[CopyMode] = None
        self.wq: "queue.Queue[Optional[bytes]]" = queue.Queue()

    def write(self, data: bytes) -> None:
        if data and not self.dead:
            self.wq.put(data)


class Client:
    """An attached client (``attach-session``): a socket the host streams
    the pane's output to and reads input / resizes from."""

    def __init__(self, cid: int, session: Session, sock, readonly: bool, pid: int):
        self.id = cid
        self.session = session
        self.sock = sock
        self.readonly = readonly
        self.pid = pid
        self.rows = 24
        self.cols = 80
        self.termname = "xterm-256color"
        self.created = _now()
        self.activity = self.created
        self.q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.queued = 0
        self.attached = True

    @property
    def name(self) -> str:
        return "winmux/%d" % self.id

    def send(self, data: bytes) -> None:
        if not self.attached or not data:
            return
        self.queued += len(data)
        if self.queued > CLIENT_QUEUE_LIMIT:
            self.attached = False
            self.q.put(None)
            return
        self.q.put(data)

    def finish(self, message: str = "") -> None:
        if message:
            self.q.put(message.encode("utf-8"))
        self.q.put(None)


class Host:
    def __init__(self, run_dir: str, idle_grace: Optional[float] = None) -> None:
        self.run_dir = run_dir
        self.lock = threading.RLock()
        self.token = secrets.token_hex(32)
        self.namespaces: Dict[str, Namespace] = {}
        self._ids = {"session": 0, "pane": 0, "window": 0, "client": 0}
        self.inflight = 0
        self.closing = False
        self.started = _now()
        self.had_session = False
        if idle_grace is None:
            idle_grace = float(os.environ.get("WINMUX_IDLE_GRACE", "30"))
        self.idle_grace = idle_grace
        self.bin_dir = os.path.join(run_dir, "bin")
        host = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:  # noqa: D401 - socketserver API
                host._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]

    # -- ids -------------------------------------------------------------------
    def next_id(self, kind: str) -> int:
        n = self._ids[kind]
        self._ids[kind] = n + 1
        return n

    # -- lifecycle -------------------------------------------------------------
    def info_path(self) -> str:
        return os.path.join(self.run_dir, "winmux.json")

    def publish(self) -> None:
        """Write the rendezvous file atomically (a client never reads half)."""
        os.makedirs(self.run_dir, exist_ok=True)
        self._write_shims()
        info = {
            "port": self.port,
            "pid": os.getpid(),
            "token": self.token,
            "proto": proto.PROTO,
            "started": self.started,
        }
        tmp = self.info_path() + ".%d.tmp" % os.getpid()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(info, f)
        os.replace(tmp, self.info_path())

    def unpublish(self) -> None:
        try:
            with open(self.info_path()) as f:
                info = json.load(f)
            if info.get("pid") == os.getpid():
                os.unlink(self.info_path())
        except (OSError, ValueError):
            pass

    def _write_shims(self) -> None:
        """A ``tmux`` on every session's PATH: launch scripts and hooks run
        ``tmux paste-buffer …`` / ``tmux display-message -t $TMUX_PANE …``
        inside the session, and on Windows there is no tmux binary. The
        shims run this package's CLI with the host's own Python."""
        os.makedirs(self.bin_dir, exist_ok=True)
        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        py = sys.executable
        cli = os.path.join(self.bin_dir, "winmux_cli.py")
        from backend.session.winmux import _bootstrap

        body = (
            "# Generated by the MindFlock session host: `tmux` inside a session.\n"
            + _bootstrap.CLI_CODE
        )
        files = {
            cli: body,
            os.path.join(self.bin_dir, "tmux"): '#!/bin/sh\nexec "%s" "%s" "%s" "$@"\n'
            % (py.replace("\\", "/"), cli.replace("\\", "/"), root.replace("\\", "/")),
            os.path.join(self.bin_dir, "tmux.cmd"): '@"%s" "%s" "%s" %%*\r\n'
            % (py, cli, root),
        }
        for path, text in files.items():
            try:
                with open(path, encoding="utf-8") as f:
                    if f.read() == text:
                        continue
            except OSError:
                pass
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            if not path.endswith((".cmd", ".py")):
                try:
                    os.chmod(path, 0o755)
                except OSError:
                    pass

    def serve(self) -> None:
        self.publish()
        threading.Thread(
            target=self._monitor, name="winmux-monitor", daemon=True
        ).start()
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self.unpublish()
            with self.lock:
                for ns in list(self.namespaces.values()):
                    for s in list(ns.sessions.values()):
                        self.destroy_session(s, "[server exited]\r\n")
            self.server.server_close()

    def _monitor(self) -> None:
        """Exit once no namespace has a session (and none is being made)."""
        while True:
            time.sleep(0.25)
            with self.lock:
                if self.closing:
                    return
                empty = not any(ns.sessions for ns in self.namespaces.values())
                idle_since_start = _now() - self.started > self.idle_grace
                if (
                    empty
                    and self.inflight == 0
                    and (self.had_session or idle_since_start)
                ):
                    self.closing = True
                    self.unpublish()
                    break
        self.server.shutdown()

    # -- network -----------------------------------------------------------------
    def _handle(self, sock: socket.socket) -> None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        buf = bytearray()
        try:
            req = json.loads(proto.recv_line(sock, buf, limit=256 << 20))
        except (ConnectionError, ValueError, OSError):
            return
        if not isinstance(req, dict) or not hmac.compare_digest(
            str(req.get("token", "")), self.token
        ):
            try:
                proto.send_json(sock, {"ok": False, "error": "bad token"})
            except OSError:
                pass
            return
        op = req.get("op")
        try:
            if op == "ping":
                proto.send_json(
                    sock, {"ok": True, "pid": os.getpid(), "proto": proto.PROTO}
                )
            elif op == "run":
                with self.lock:
                    if self.closing:
                        proto.send_json(sock, {"retry": True})
                        return
                    self.inflight += 1
                try:
                    rc, out, err = self.run(req)
                finally:
                    with self.lock:
                        self.inflight -= 1
                proto.send_json(
                    sock,
                    {
                        "ok": True,
                        "rc": rc,
                        "out": proto.b64(out),
                        "err": proto.b64(err),
                    },
                )
            elif op == "attach":
                self._attach(sock, buf, req)
            else:
                proto.send_json(sock, {"ok": False, "error": "unknown op"})
        except OSError:
            pass

    # -- commands ----------------------------------------------------------------
    def run(self, req: dict) -> Tuple[int, bytes, bytes]:
        """A tmux argv (without ``tmux``) — a chain, run in order; the first
        failure stops it, as in tmux."""
        from backend.session.winmux import commands

        argv = [str(a) for a in req.get("argv") or []]
        err: List[bytes] = []
        try:
            flags, cmdv = proto.parse_top(argv)
        except CmdError as e:
            return 1, b"", (str(e) + "\n").encode()
        ns_name = proto.namespace_of(flags)
        chain = proto.split_chain(cmdv) or [["new-session"]]
        with self.lock:
            ns = self.namespaces.get(ns_name)
            if ns is None:
                starts = any(
                    proto.canonical(c[0]) in ("new-session", "start-server")
                    for c in chain
                )
                if not starts:
                    return (
                        1,
                        b"",
                        (
                            "no server running on %s\n" % proto.socket_label(ns_name)
                        ).encode(),
                    )
                ns = Namespace(ns_name, req.get("env") or dict(os.environ))
                self.namespaces[ns_name] = ns
            ctx = Ctx(
                self,
                ns,
                cwd=str(req.get("cwd") or ""),
                env={str(k): str(v) for k, v in (req.get("env") or {}).items()},
                stdin=proto.unb64(req.get("stdin")),
                pid=int(req.get("pid") or 0),
            )
            rc = 0
            for cmd in chain:
                try:
                    commands.execute(ctx, cmd)
                except CmdError as e:
                    err.append((str(e) + "\n").encode("utf-8", "replace"))
                    rc = 1
                    break
            self._reap_namespace(ns)
        return rc, b"".join(ctx.out), b"".join(err)

    def run_hook_commands(
        self, ns: Namespace, commands_text: List[str], session
    ) -> None:
        """Run hook commands (tmux command strings) off the caller's stack."""
        from backend.session.winmux import commands

        def go():
            with self.lock:
                if self.namespaces.get(ns.name) is not ns:
                    return
                ctx = Ctx(self, ns)
                ctx.hook_session = session
                for text in commands_text:
                    for cmd in commands.parse_command_string(text):
                        try:
                            commands.execute(ctx, cmd)
                        except CmdError:
                            break
                self._reap_namespace(ns)

        threading.Thread(target=go, name="winmux-hook", daemon=True).start()

    def fire(self, ns: Namespace, name: str, session: Optional[Session] = None) -> None:
        cmds: List[str] = []
        if session is not None and name in session.hooks:
            cmds = session.hooks[name]
        elif name in ns.hooks:
            cmds = ns.hooks[name]
        if cmds:
            self.run_hook_commands(ns, list(cmds), session)

    def _reap_namespace(self, ns: Namespace) -> None:
        """A namespace with no sessions is a tmux server that has exited:
        its options, environment, buffers and hooks go with it."""
        if not ns.sessions and self.namespaces.get(ns.name) is ns:
            del self.namespaces[ns.name]

    # -- options -------------------------------------------------------------------
    def option(self, ns: Namespace, session: Optional[Session], name: str) -> str:
        if session is not None and name in session.options:
            return session.options[name]
        if name in ns.options:
            return ns.options[name]
        return OPTION_DEFAULTS.get(name, "")

    def option_on(self, ns: Namespace, session: Optional[Session], name: str) -> bool:
        return self.option(ns, session, name) in ("on", "yes", "1", "true")

    def apply_options(self, session: Session) -> None:
        """Make stored options take effect on a session's pane."""
        pane = session.pane
        if pane is None:
            return
        try:
            pane.term.set_history_limit(
                int(self.option(session.ns, session, "history-limit"))
            )
        except ValueError:
            pass
        if self.option(session.ns, session, "window-size") != "manual":
            session.manual_size = None
            self.fit_size(session)

    # -- panes ---------------------------------------------------------------------
    def spawn_pane(
        self,
        session: Session,
        argv: List[str],
        cwd: str,
        env: Dict[str, str],
        rows: int,
        cols: int,
    ) -> Pane:
        pid_n = self.next_id("pane")
        env = dict(env)
        env["TMUX"] = "winmux,%d,%d" % (os.getpid(), 0)
        env["TMUX_PANE"] = "%%%d" % pid_n
        env["TERM"] = (
            self.option(session.ns, session, "default-terminal") or "xterm-256color"
        )
        path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
        env[path_key] = self.bin_dir + os.pathsep + env.get(path_key, "")
        try:
            hist = int(self.option(session.ns, session, "history-limit"))
        except ValueError:
            hist = DEFAULT_HISTORY
        term = Term(cols, rows, hist)
        pty = ptys.spawn(argv, cwd, env, rows, cols)
        pane = Pane(pid_n, session, pty, term)
        pane.start_path = cwd
        session.pane = pane
        threading.Thread(
            target=self._pump, args=(pane,), name="winmux-pump", daemon=True
        ).start()
        threading.Thread(
            target=self._writer, args=(pane,), name="winmux-in", daemon=True
        ).start()
        return pane

    def _writer(self, pane: Pane) -> None:
        while True:
            data = pane.wq.get()
            if data is None:
                return
            try:
                pane.pty.write(data)
            except Exception:  # noqa: BLE001 - the child went away
                pass

    def _pump(self, pane: Pane) -> None:
        import codecs

        dec = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            data = pane.pty.read()
            if not data:
                break
            text = dec.decode(data)
            if not text:
                continue
            with self.lock:
                if pane.gone:
                    continue
                client_text, reply = pane.term.feed(text)
                if reply:
                    pane.write(reply.encode("utf-8"))
                if client_text and pane.mode is None:
                    out = client_text.encode("utf-8", "surrogateescape")
                    for c in pane.session.clients:
                        c.send(out)
        status = pane.pty.exit_status()
        pane.pty.close()
        pane.wq.put(None)
        with self.lock:
            pane.dead = True
            pane.dead_status = status
            if pane.gone:
                return
            session = pane.session
            ns = session.ns
            if self.option_on(ns, session, "remain-on-exit"):
                self.fire(ns, "pane-died", session)
                return
            self.fire(ns, "pane-exited", session)
            self.destroy_session(session, "[exited]\r\n")
            self._reap_namespace(ns)

    def destroy_session(self, session: Session, message: str = "[exited]\r\n") -> None:
        ns = session.ns
        if ns.sessions.get(session.name) is session:
            del ns.sessions[session.name]
        pane = session.pane
        if pane is not None and not pane.gone:
            pane.gone = True
            if not pane.dead:
                threading.Thread(target=pane.pty.kill, daemon=True).start()
        for c in list(session.clients):
            c.attached = False
            c.finish(message)
        session.clients = []
        self.fire(ns, "session-closed", None)

    def resize_pane(self, session: Session, rows: int, cols: int) -> None:
        pane = session.pane
        if pane is None or pane.gone:
            return
        rows, cols = max(1, int(rows)), max(1, int(cols))
        if (pane.term.rows, pane.term.cols) == (rows, cols):
            return
        pane.term.resize(rows, cols)
        if not pane.dead:
            try:
                pane.pty.resize(rows, cols)
            except Exception:  # noqa: BLE001
                pass
        if pane.mode is not None:
            pane.mode.offset = min(pane.mode.offset, pane.term.history_size)
            self.paint_clients(session)

    def fit_size(self, session: Session, prefer: Optional[Client] = None) -> None:
        """Size the window by ``window-size`` (tmux's default ``latest``:
        the client that was most recently active)."""
        if session.manual_size is not None:
            return
        policy = self.option(session.ns, session, "window-size")
        clients = [c for c in session.clients if c.attached]
        if not clients or policy == "manual":
            return
        if policy == "largest":
            rows, cols = max(c.rows for c in clients), max(c.cols for c in clients)
        elif policy == "smallest":
            rows, cols = min(c.rows for c in clients), min(c.cols for c in clients)
        else:
            c = prefer or max(clients, key=lambda c: c.activity)
            rows, cols = c.rows, c.cols
        self.resize_pane(session, rows, cols)

    def paint_clients(self, session: Session) -> None:
        """Redraw every client: the copy-mode view, or the live screen."""
        pane = session.pane
        if pane is None:
            return
        data = self.paint_for(pane, history=0)
        for c in session.clients:
            c.send(data)

    def paint_for(self, pane: Pane, history: int) -> bytes:
        if pane.mode is not None:
            return pane.term.view(pane.mode.offset).encode("utf-8", "surrogateescape")
        return pane.term.repaint(history).encode("utf-8", "surrogateescape")

    # -- copy mode -------------------------------------------------------------------
    def mode_enter(self, pane: Pane, exit_on_bottom: bool) -> None:
        if pane.mode is None:
            pane.mode = CopyMode(exit_on_bottom)
            self.paint_clients(pane.session)

    def mode_exit(self, pane: Pane) -> None:
        if pane.mode is not None:
            pane.mode = None
            self.paint_clients(pane.session)

    def mode_command(self, pane: Pane, cmd: str, n: int = 1) -> None:
        m = pane.mode
        if m is None:
            return
        rows = pane.term.rows
        hsize = pane.term.history_size
        n = max(1, n)
        step = {
            "scroll-up": n,
            "scroll-down": -n,
            "cursor-up": n,
            "cursor-down": -n,
            "page-up": rows * n,
            "page-down": -rows * n,
            "halfpage-up": (rows // 2) * n,
            "halfpage-down": -(rows // 2) * n,
            "scroll-up-and-cancel": n,
            "scroll-down-and-cancel": -n,
            "page-down-and-cancel": -rows * n,
        }
        if cmd in ("cancel", "copy-pipe-and-cancel", "copy-selection-and-cancel"):
            self.mode_exit(pane)
            return
        if cmd == "history-top":
            m.offset = hsize
        elif cmd == "history-bottom":
            m.offset = 0
        elif cmd in step:
            m.offset = max(0, min(hsize, m.offset + step[cmd]))
        else:
            return  # selection, search, jumps: nothing MindFlock relies on
        if (
            m.offset == 0
            and (m.exit_on_bottom or cmd.endswith("-and-cancel"))
            and (step.get(cmd, 0) < 0 or cmd == "history-bottom")
        ):
            self.mode_exit(pane)
            return
        self.paint_clients(pane.session)

    def mode_key(self, pane: Pane, data: bytes) -> None:
        """Keys typed at a pane in copy mode: tmux's emacs/vi table, the part
        a person uses to read back and leave."""
        table = {
            b"q": "cancel",
            b"\x1b": "cancel",
            b"\x03": "cancel",
            b"\r": "cancel",
            b"\x1b[A": "scroll-up",
            b"\x1bOA": "scroll-up",
            b"k": "scroll-up",
            b"\x1b[B": "scroll-down",
            b"\x1bOB": "scroll-down",
            b"j": "scroll-down",
            b"\x1b[5~": "page-up",
            b"\x1b[6~": "page-down",
            b"g": "history-top",
            b"G": "history-bottom",
        }
        cmd = table.get(data)
        if cmd:
            self.mode_command(pane, cmd)

    # -- attach --------------------------------------------------------------------
    def _attach(self, sock: socket.socket, buf: bytearray, req: dict) -> None:
        from backend.session.winmux import commands

        ns_name = str(req.get("ns") or "default")
        with self.lock:
            ns = self.namespaces.get(ns_name)
            if ns is None:
                proto.send_json(
                    sock,
                    {
                        "ok": False,
                        "error": "no server running on %s"
                        % proto.socket_label(ns_name),
                    },
                )
                return
            ctx = Ctx(
                self,
                ns,
                env={str(k): str(v) for k, v in (req.get("env") or {}).items()},
            )
            try:
                session = commands.find_session(ctx, req.get("target"))
            except CmdError as e:
                proto.send_json(sock, {"ok": False, "error": str(e)})
                return
            client = Client(
                self.next_id("client"),
                session,
                sock,
                bool(req.get("readonly")),
                int(req.get("pid") or 0),
            )
            client.rows = max(1, int(req.get("rows") or 24))
            client.cols = max(1, int(req.get("cols") or 80))
            client.termname = str(req.get("termname") or "xterm-256color")
            if req.get("detach_others"):
                for other in list(session.clients):
                    self.detach_client(
                        other, "[detached (from session %s)]\r\n" % session.name
                    )
            proto.send_json(
                sock, {"ok": True, "session": session.name, "pid": os.getpid()}
            )
            session.clients.append(client)
            session.last_attached = _now()
            session.activity = _now()
            self.fit_size(session, prefer=client)
            if session.pane is not None:
                client.q.put(self.paint_for(session.pane, REPAINT_HISTORY))
            self.fire(ns, "client-attached", session)
        threading.Thread(
            target=self._client_writer, args=(client,), name="winmux-out", daemon=True
        ).start()
        try:
            self._client_reader(client, sock, buf)
        finally:
            with self.lock:
                if client in client.session.clients:
                    self.detach_client(client, "")
                    self.fire(client.session.ns, "client-detached", client.session)
                else:
                    client.finish("")

    def detach_client(self, client: Client, message: str) -> None:
        session = client.session
        if client in session.clients:
            session.clients.remove(client)
        if client.attached:
            client.attached = False
            client.finish(message)
            self.fit_size(session)

    def _client_writer(self, client: Client) -> None:
        sock = client.sock
        while True:
            data = client.q.get()
            if data is None:
                break
            client.queued -= len(data)
            try:
                sock.sendall(data)
            except OSError:
                break
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _client_reader(
        self, client: Client, sock: socket.socket, buf: bytearray
    ) -> None:
        def need(n: int) -> Optional[bytes]:
            while len(buf) < n:
                try:
                    chunk = sock.recv(1 << 16)
                except OSError:
                    return None
                if not chunk:
                    return None
                buf.extend(chunk)
            out = bytes(buf[:n])
            del buf[:n]
            return out

        while client.attached:
            head = need(5)
            if head is None:
                return
            kind, size = head[:1], struct.unpack(">I", head[1:])[0]
            payload = need(size) if size else b""
            if payload is None:
                return
            if kind == b"x":
                return
            with self.lock:
                if not client.attached:
                    return
                session = client.session
                if kind == b"r" and len(payload) == 4:
                    client.rows, client.cols = struct.unpack(">HH", payload)
                    client.activity = _now()
                    self.fit_size(session, prefer=client)
                elif kind == b"d" and not client.readonly:
                    client.activity = _now()
                    session.activity = client.activity
                    pane = session.pane
                    if pane is None or pane.gone:
                        continue
                    self.fit_size(session, prefer=client)
                    if pane.mode is not None:
                        self.mode_key(pane, payload)
                    else:
                        pane.write(payload)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m``-less entry (see :mod:`._bootstrap`): ``host.py RUN_DIR``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    run_dir = argv[0] if argv else proto.run_dir()
    os.makedirs(run_dir, exist_ok=True)
    log = os.path.join(run_dir, "winmux.log")
    try:
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(fd, 2)
        os.close(fd)
    except OSError:
        pass
    host = Host(run_dir)
    host.serve()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
