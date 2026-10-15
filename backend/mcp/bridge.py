"""The shared-MCP bridge: one stdio MCP server process, many agent windows.

Every Claude Code window spawns its own copy of every stdio MCP server in the
user's config — ~500 MB of identical node / uv processes per window on a busy
machine. For a server that is safe to share (stateless, configured entirely by
its own command/args/env, never asks the client anything), this daemon runs it
ONCE and serves it to every window over MCP streamable HTTP on 127.0.0.1:

    POST/GET/DELETE  http://127.0.0.1:<port>/mcp/<name>
    Authorization:   Bearer <per-machine token>

Stdlib only (like :mod:`backend.mcp`), one process per machine, started lazily
by the launch side (:mod:`backend.providers.mcp_share`), detached into its own
session so it outlives a MindFlock server restart the way the tmux sessions
using it do. It exits on its own once nothing has used it for
:data:`IDLE_EXIT_S` (and on SIGTERM), killing its upstream process groups.

Multiplexing rules — what makes N clients look like ONE client upstream:

* ``initialize`` reaches the upstream once (with the first client's protocol
  version and clientInfo, but NO client capabilities — no roots, sampling or
  elicitation, so a well-behaved server never asks for them). Every client's
  ``initialize`` is answered from the cached result under the client's own id,
  and ``notifications/initialized`` is swallowed.
* Request ids are remapped to bridge-unique upstream ids so responses route
  back to the right client; so are ``_meta.progressToken`` (two clients may pick
  the same token) and the ``requestId`` of ``notifications/cancelled``.
* ``ping`` and ``logging/setLevel`` are answered locally (one client's level
  must not change another's); ``resources/subscribe`` / ``unsubscribe`` are
  reference-counted per URI.
* Server notifications — ``*/list_changed``, ``notifications/message``,
  ``notifications/resources/updated`` — fan out to every client of that
  upstream (over the client's GET stream).
* Server→client REQUESTS (``sampling/createMessage``, ``elicitation/create``,
  ``roots/list``, anything but ``ping``) cannot be multiplexed — which window
  would answer? — so the bridge answers them itself with ``-32601`` and counts
  them in ``/health``. A server that needs them must not be shared.
* The upstream is restarted (and re-initialized with the cached parameters) on
  the next request after it dies; requests in flight fail with ``-32603`` and
  clients are told the lists may have changed. Clients keep their sessions.
  More than :data:`RESTART_BURST` crashes inside :data:`RESTART_WINDOW_S` and it
  fails fast instead of crash-looping.
* Upstream stderr goes to ``<state dir>/logs/<name>.log``.

Lifetime: an upstream starts on the first client ``initialize`` and stops once
it has had no live client for :data:`UPSTREAM_IDLE_S`. A client is live while
it holds its GET notification stream open (Claude Code keeps one for the life
of the window) or has used its session within :data:`SESSION_IDLE_S`.

Definitions come from ``<state dir>/servers.json`` (0600, written by the launch
side): ``{name: {"command", "args", "env", "cwd"}}``. An upstream is keyed by
name AND definition digest, so an edited definition starts a fresh upstream for
new sessions while the old one drains.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import itertools
import json
import os
import queue
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "BRIDGE_FILE",
    "SERVERS_FILE",
    "TOKEN_ENV",
    "definition_digest",
    "Upstream",
    "Hub",
    "make_server",
    "main",
]

#: Files in the state directory.
BRIDGE_FILE = "bridge.json"  # {pid, port, token, version, started_at}
SERVERS_FILE = "servers.json"  # {name: definition}
LOCK_FILE = "bridge.lock"
LOG_DIR = "logs"

#: The bearer token arrives in this env var (never argv: ``ps`` shows argv).
TOKEN_ENV = "MINDFLOCK_MCP_BRIDGE_TOKEN"

#: Protocol revision offered upstream when the client names none.
DEFAULT_PROTOCOL = "2025-06-18"

UPSTREAM_INIT_TIMEOUT_S = 60.0
REQUEST_TIMEOUT_S = 3600.0
KEEPALIVE_S = 20.0
SESSION_IDLE_S = 6 * 3600.0
UPSTREAM_IDLE_S = 600.0
IDLE_EXIT_S = 900.0
REAP_EVERY_S = 30.0
RESTART_WINDOW_S = 60.0
RESTART_BURST = 5
#: ``MINDFLOCK_MCP_BRIDGE_DEBUG=1`` logs every HTTP request line to stderr.
_DEBUG = (os.environ.get("MINDFLOCK_MCP_BRIDGE_DEBUG") or "").strip() in (
    "1",
    "true",
    "yes",
)

#: Notifications parked for a client that has no GET stream open right now.
MAX_PARKED = 200

#: The error code clients get for things the bridge refuses.
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
INVALID_REQUEST = -32600

#: Methods whose notification fans out to every client of the upstream.
_BROADCAST = (
    "notifications/tools/list_changed",
    "notifications/prompts/list_changed",
    "notifications/resources/list_changed",
    "notifications/resources/updated",
    "notifications/message",
)


def _log(msg: str) -> None:
    sys.stderr.write("%s mcp-bridge: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    sys.stderr.flush()


def definition_digest(definition: dict) -> str:
    """A short stable digest of a server definition (command/args/env/cwd)."""
    canon = json.dumps(
        {k: definition.get(k) for k in ("command", "args", "env", "cwd")},
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _result(mid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


# --------------------------------------------------------------------------- #
# Upstream: one stdio MCP server process
# --------------------------------------------------------------------------- #
class _Pending:
    """One client request in flight upstream."""

    __slots__ = ("session", "client_id", "out", "token", "done")

    def __init__(self, session: "Session", client_id: Any, out: "queue.Queue"):
        self.session = session
        self.client_id = client_id
        self.out = out  # receives progress notifications, then the response
        self.token: Any = None  # the client's own progressToken, if any
        self.done = False


class Upstream:
    """One stdio MCP server process shared by many :class:`Session` s.

    Thread-safe. ``popen`` is injectable for tests."""

    def __init__(
        self,
        name: str,
        definition: dict,
        log_path: str = "",
        popen: Callable[..., Any] = subprocess.Popen,
    ):
        self.name = name
        self.definition = dict(definition)
        self.digest = definition_digest(definition)
        self.log_path = log_path
        self._popen = popen
        self._lock = threading.RLock()  # state; never held while waiting
        self._start_lock = threading.Lock()  # serializes (re)starts
        self._write_lock = threading.Lock()
        self._proc: Any = None
        self._ready = False
        self._init_params: Optional[dict] = None
        self.init_result: Optional[dict] = None
        self._ids = itertools.count(1)
        self._pending: Dict[int, _Pending] = {}
        self._tokens: Dict[str, _Pending] = {}
        self._init_wait: Optional["queue.Queue"] = None
        self._subs: Dict[str, set] = {}
        self.sessions: Dict[str, "Session"] = {}
        self.starts = 0
        self.crashes: List[float] = []
        self.refused: Dict[str, int] = {}
        self.last_used = time.time()
        self.idle_since: Optional[float] = None
        self.opening = 0  # sessions being opened right now (the reaper waits)

    # -- process ------------------------------------------------------------ #
    @property
    def pid(self) -> Optional[int]:
        proc = self._proc
        return getattr(proc, "pid", None) if proc is not None else None

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _spawn(self) -> None:
        d = self.definition
        argv = [str(d.get("command") or "")] + [str(a) for a in d.get("args") or []]
        if not argv[0]:
            raise RuntimeError("server %r has no command" % self.name)
        env = dict(os.environ)
        env.pop(TOKEN_ENV, None)
        env.update({str(k): str(v) for k, v in (d.get("env") or {}).items()})
        cwd = str(d.get("cwd") or "") or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        logf: Any = subprocess.DEVNULL
        if self.log_path:
            os.makedirs(os.path.dirname(self.log_path), mode=0o700, exist_ok=True)
            logf = open(self.log_path, "ab", buffering=0)
            logf.write(
                (
                    "\n==== %s start #%d: %s ====\n"
                    % (time.strftime("%Y-%m-%dT%H:%M:%S"), self.starts + 1, argv[0])
                ).encode()
            )
        try:
            self._proc = self._popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=logf,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        finally:
            if logf is not subprocess.DEVNULL:
                logf.close()
        self.starts += 1
        proc = self._proc
        threading.Thread(
            target=self._read_loop,
            args=(proc,),
            name="mcp-up-%s" % self.name,
            daemon=True,
        ).start()

    def stop(self) -> None:
        """Kill the process group (the server may be ``npx`` → ``node``)."""
        with self._lock:
            proc, self._proc = self._proc, None
            self._ready = False
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (OSError, AttributeError):
                try:
                    proc.send_signal(sig)
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.wait(timeout=5)
                break
            except Exception:  # noqa: BLE001
                continue
        self._fail_pending("shared MCP server %r stopped" % self.name)

    # -- I/O ---------------------------------------------------------------- #
    def _send(self, msg: dict) -> None:
        proc = self._proc
        if proc is None:
            raise BrokenPipeError("upstream not running")
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            proc.stdin.write(data)
            proc.stdin.flush()

    def _read_loop(self, proc) -> None:
        try:
            for raw in proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue  # stray non-protocol output
                if isinstance(msg, list):
                    for m in msg:
                        if isinstance(m, dict):
                            self._on_message(m)
                elif isinstance(msg, dict):
                    self._on_message(msg)
        except Exception as e:  # noqa: BLE001
            _log("%s: reader error %s" % (self.name, type(e).__name__))
        finally:
            self._on_exit(proc)

    def _on_exit(self, proc) -> None:
        if self._proc is not proc:
            return  # an old incarnation (already replaced or stopped)
        init_wait = self._init_wait
        if init_wait is not None:  # before the lock: a starter may hold it
            init_wait.put(
                _error(0, INTERNAL_ERROR, "upstream exited during initialize")
            )
        with self._lock:
            if self._proc is not proc:
                return
            self._proc = None
            self._ready = False
            self.crashes.append(time.time())
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        _log(
            "%s: upstream exited (code %s)"
            % (self.name, getattr(proc, "returncode", None))
        )
        self._fail_pending("shared MCP server %r restarted; retry the call" % self.name)

    def _fail_pending(self, message: str) -> None:
        with self._lock:
            pend = list(self._pending.values())
            self._pending.clear()
            self._tokens.clear()
        for p in pend:
            if not p.done:
                p.out.put(_error(p.client_id, INTERNAL_ERROR, message))
                p.done = True

    def _on_message(self, msg: dict) -> None:
        method = msg.get("method")
        mid = msg.get("id")
        if method is None:
            # A response to one of ours.
            if mid == 0 and self._init_wait is not None:
                self._init_wait.put(msg)
                return
            with self._lock:
                p = self._pending.pop(mid, None) if isinstance(mid, int) else None
                if p is not None and p.token is not None:
                    self._tokens.pop(self._token_for(mid), None)
            if p is None or p.done:
                return
            out = dict(msg)
            out["id"] = p.client_id
            p.out.put(out)
            p.done = True  # after the put: a waiter reads done+empty as cancelled
            return
        if mid is not None:
            # A server→client REQUEST. Only ping can be answered for everyone.
            if method == "ping":
                self._safe_send(_result(mid, {}))
                return
            with self._lock:
                self.refused[method] = self.refused.get(method, 0) + 1
            _log("%s: refused server->client request %s" % (self.name, method))
            self._safe_send(
                _error(
                    mid,
                    METHOD_NOT_FOUND,
                    "%s is not available through MindFlock's shared MCP bridge "
                    "(this server should not be shared)" % method,
                )
            )
            return
        # A notification.
        if method == "notifications/progress":
            params = msg.get("params") or {}
            with self._lock:
                p = self._tokens.get(str(params.get("progressToken")))
            if p is not None and not p.done:
                note = dict(msg)
                note["params"] = dict(params, progressToken=p.token)
                p.out.put(note)
            return
        if method in _BROADCAST:
            targets = self._targets(msg)
            for s in targets:
                s.notify(msg)

    def _targets(self, msg: dict) -> list:
        with self._lock:
            if msg.get("method") == "notifications/resources/updated":
                uri = str((msg.get("params") or {}).get("uri", ""))
                subs = self._subs.get(uri)
                if subs:
                    return [s for s in self.sessions.values() if s.sid in subs]
            return list(self.sessions.values())

    def _safe_send(self, msg: dict) -> None:
        try:
            self._send(msg)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _token_for(uid: int) -> str:
        return "mfb-%d" % uid

    # -- lifecycle ---------------------------------------------------------- #
    def ensure_ready(self, client_params: Optional[dict]) -> dict:
        """Start + initialize the upstream if it is not running; return the
        cached ``initialize`` result. Raises ``RuntimeError`` with a message
        fit for the client.

        Starters serialize on ``_start_lock``; the state lock is only taken in
        short bursts, never across the wait — the reader thread needs it to
        deliver anything the server sends before its ``initialize`` reply."""
        if self._ready and self.running:
            return self.init_result or {}
        with self._start_lock:
            with self._lock:
                if self._ready and self.running:
                    return self.init_result or {}
                now = time.time()
                self.crashes = [t for t in self.crashes if now - t < RESTART_WINDOW_S]
                if len(self.crashes) >= RESTART_BURST:
                    raise RuntimeError(
                        "shared MCP server %r keeps exiting; see %s"
                        % (self.name, self.log_path or "its log")
                    )
                if self._init_params is None:
                    params = dict(client_params or {})
                    params["protocolVersion"] = str(
                        params.get("protocolVersion") or DEFAULT_PROTOCOL
                    )
                    params["capabilities"] = {}  # no roots / sampling / elicitation
                    params.setdefault(
                        "clientInfo", {"name": "mindflock-mcp-bridge", "version": "1"}
                    )
                    self._init_params = params
                restarted = self.init_result is not None
                stale = self.running
            if stale:
                self.stop()  # running but never finished initializing
            wait: "queue.Queue" = queue.Queue()
            self._init_wait = wait
            try:
                with self._lock:
                    self._spawn()
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": self._init_params,
                    }
                )
                try:
                    reply = wait.get(timeout=UPSTREAM_INIT_TIMEOUT_S)
                except queue.Empty:
                    reply = _error(0, INTERNAL_ERROR, "upstream initialize timed out")
            except Exception as e:  # noqa: BLE001
                reply = _error(0, INTERNAL_ERROR, "could not start: %s" % e)
            finally:
                self._init_wait = None
            if "result" not in reply:
                err = (reply.get("error") or {}).get("message") or "initialize failed"
                self.stop()
                with self._lock:
                    self.crashes.append(time.time())
                raise RuntimeError("shared MCP server %r: %s" % (self.name, err))
            self._safe_send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            with self._lock:
                self.init_result = reply["result"]
                self._ready = True
                subs = list(self._subs)
                sessions = list(self.sessions.values())
        for uri in subs:  # a fresh process knows no subscriptions
            self._safe_send(
                {
                    "jsonrpc": "2.0",
                    "id": -1,
                    "method": "resources/subscribe",
                    "params": {"uri": uri},
                }
            )
        if restarted:  # its lists may differ now: have every client refetch
            caps = (self.init_result or {}).get("capabilities") or {}
            for kind in ("tools", "prompts", "resources"):
                if kind in caps:
                    note = {
                        "jsonrpc": "2.0",
                        "method": "notifications/%s/list_changed" % kind,
                    }
                    for sess in sessions:
                        sess.notify(note)
        return self.init_result or {}

    # -- requests ----------------------------------------------------------- #
    def forward(
        self, session: "Session", msg: dict, out: "queue.Queue"
    ) -> Optional[_Pending]:
        """Send a client request upstream under a fresh id. The response (and
        any progress) arrives on ``out`` with the client's id restored."""
        self.ensure_ready(None)
        uid = next(self._ids)
        p = _Pending(session, msg.get("id"), out)
        fwd = dict(msg)
        fwd["id"] = uid
        params = msg.get("params")
        if isinstance(params, dict) and isinstance(params.get("_meta"), dict):
            meta = params["_meta"]
            if "progressToken" in meta:
                p.token = meta["progressToken"]
                fwd["params"] = dict(
                    params, _meta=dict(meta, progressToken=self._token_for(uid))
                )
        with self._lock:
            self._pending[uid] = p
            if p.token is not None:
                self._tokens[self._token_for(uid)] = p
            self.last_used = time.time()
        try:
            self._send(fwd)
        except Exception:  # noqa: BLE001
            with self._lock:
                self._pending.pop(uid, None)
                self._tokens.pop(self._token_for(uid), None)
            out.put(
                _error(
                    p.client_id,
                    INTERNAL_ERROR,
                    "shared MCP server %r is not running" % self.name,
                )
            )
            p.done = True
        return p

    def cancel(self, session: "Session", client_id: Any, reason: str = "") -> None:
        """Forward ``notifications/cancelled`` for ``session``'s ``client_id``."""
        with self._lock:
            hit = None
            for uid, p in self._pending.items():
                if p.session is session and p.client_id == client_id and not p.done:
                    hit = uid
                    break
            if hit is None:
                return
            p = self._pending.pop(hit)
            self._tokens.pop(self._token_for(hit), None)
            p.done = True
        params: dict = {"requestId": hit}
        if reason:
            params["reason"] = reason
        self._safe_send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params}
        )

    def drop_pending(self, p: _Pending) -> None:
        """A client went away mid-request: cancel it upstream."""
        if not p.done:
            self.cancel(p.session, p.client_id, "client disconnected")

    def subscribe(self, session: "Session", msg: dict, out: "queue.Queue") -> None:
        uri = str((msg.get("params") or {}).get("uri", ""))
        with self._lock:
            subs = self._subs.setdefault(uri, set())
            first = not subs
            subs.add(session.sid)
        if first:
            self.forward(session, msg, out)
        else:
            out.put(_result(msg.get("id"), {}))

    def unsubscribe(self, session: "Session", msg: dict, out: "queue.Queue") -> None:
        uri = str((msg.get("params") or {}).get("uri", ""))
        with self._lock:
            subs = self._subs.get(uri) or set()
            subs.discard(session.sid)
            last = not subs
            if last:
                self._subs.pop(uri, None)
        if last:
            self.forward(session, msg, out)
        else:
            out.put(_result(msg.get("id"), {}))

    def detach(self, session: "Session") -> None:
        with self._lock:
            self.sessions.pop(session.sid, None)
            gone = [
                u
                for u, subs in self._subs.items()
                if session.sid in subs and len(subs) == 1
            ]
            for u, subs in self._subs.items():
                subs.discard(session.sid)
            for u in gone:
                self._subs.pop(u, None)
            pend = [p for p in self._pending.values() if p.session is session]
        for u in gone:
            self._safe_send(
                {
                    "jsonrpc": "2.0",
                    "id": -1,
                    "method": "resources/unsubscribe",
                    "params": {"uri": u},
                }
            )
        for p in pend:
            self.drop_pending(p)

    def status(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "digest": self.digest,
                "pid": self.pid if self.running else None,
                "running": self.running,
                "sessions": len(self.sessions),
                "starts": self.starts,
                "in_flight": len(self._pending),
                "refused_requests": dict(self.refused),
                "log": self.log_path,
            }


# --------------------------------------------------------------------------- #
# Client sessions
# --------------------------------------------------------------------------- #
class Session:
    """One client (one agent window) of one :class:`Upstream`."""

    def __init__(self, upstream: Upstream):
        self.sid = uuid.uuid4().hex
        self.upstream = upstream
        self.created = time.time()
        self.last_seen = self.created
        self.streams = 0  # open GET streams
        self.in_flight = 0
        self._lock = threading.Lock()
        self._listeners: List["queue.Queue"] = []
        self._parked: List[dict] = []

    def touch(self) -> None:
        self.last_seen = time.time()

    def busy(self, delta: int) -> None:
        with self._lock:
            self.in_flight += delta
        self.touch()

    def live(self, now: float) -> bool:
        return (
            self.streams > 0
            or self.in_flight > 0
            or now - self.last_seen < SESSION_IDLE_S
        )

    def notify(self, msg: dict) -> None:
        with self._lock:
            if self._listeners:
                for q in self._listeners:
                    q.put(msg)
            elif len(self._parked) < MAX_PARKED:
                self._parked.append(msg)

    def listen(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue()
        with self._lock:
            self._listeners.append(q)
            self.streams += 1
            for m in self._parked:
                q.put(m)
            self._parked.clear()
        return q

    def unlisten(self, q: "queue.Queue") -> None:
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)
                self.streams -= 1
        self.touch()


# --------------------------------------------------------------------------- #
# The hub: definitions, upstreams, sessions, idle reaping
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(
        self,
        state_dir: str,
        popen: Callable[..., Any] = subprocess.Popen,
        definitions: Optional[Callable[[], dict]] = None,
    ):
        self.state_dir = state_dir
        self._popen = popen
        self._definitions = definitions or self._read_definitions
        self._lock = threading.Lock()
        self.upstreams: Dict[Tuple[str, str], Upstream] = {}
        self.sessions: Dict[str, Session] = {}
        self.last_activity = time.time()

    def _read_definitions(self) -> dict:
        try:
            with open(
                os.path.join(self.state_dir, SERVERS_FILE), encoding="utf-8"
            ) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def definition(self, name: str) -> Optional[dict]:
        d = self._definitions().get(name)
        return d if isinstance(d, dict) and d.get("command") else None

    def open_session(self, name: str, params: Optional[dict]) -> Tuple[Session, dict]:
        """A new client session on ``name``'s upstream (started if need be)."""
        d = self.definition(name)
        if d is None:
            raise KeyError(name)
        key = (name, definition_digest(d))
        with self._lock:
            up = self.upstreams.get(key)
            if up is None:
                log_path = os.path.join(
                    self.state_dir, LOG_DIR, _safe_name(name) + ".log"
                )
                up = Upstream(name, d, log_path, popen=self._popen)
                self.upstreams[key] = up
            up.opening += 1  # the reaper must not stop it under us
            up.idle_since = None
            self.last_activity = time.time()
        try:
            result = up.ensure_ready(params)
            s = Session(up)
            with self._lock:
                self.sessions[s.sid] = s
                with up._lock:
                    up.sessions[s.sid] = s
        finally:
            with self._lock:
                up.opening -= 1
        return s, result

    def session(self, sid: str) -> Optional[Session]:
        with self._lock:
            s = self.sessions.get(sid or "")
            if s is not None:
                self.last_activity = time.time()
        return s

    def close_session(self, sid: str) -> bool:
        with self._lock:
            s = self.sessions.pop(sid or "", None)
        if s is None:
            return False
        s.upstream.detach(s)
        return True

    def reap(self, now: Optional[float] = None) -> None:
        """Expire dead sessions, stop upstreams idle past their grace."""
        now = time.time() if now is None else now
        with self._lock:
            dead = [sid for sid, s in self.sessions.items() if not s.live(now)]
        for sid in dead:
            self.close_session(sid)
        stopping = []
        with self._lock:
            for key, up in list(self.upstreams.items()):
                with up._lock:
                    if up.sessions or up._pending or up.opening:
                        up.idle_since = None
                        continue
                    if up.idle_since is None:
                        up.idle_since = now
                        continue
                    if now - up.idle_since < UPSTREAM_IDLE_S:
                        continue
                del self.upstreams[key]  # no new session can find it now
                stopping.append(up)
        for up in stopping:
            _log(
                "%s: no clients for %ds, stopping"
                % (up.name, int(now - (up.idle_since or now)))
            )
            up.stop()
        with self._lock:
            if self.sessions or any(u.running for u in self.upstreams.values()):
                self.last_activity = max(self.last_activity, now)

    def idle_for(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            if self.sessions or self.upstreams:
                return 0.0
            return now - self.last_activity

    def shutdown(self) -> None:
        with self._lock:
            ups = list(self.upstreams.values())
            self.upstreams.clear()
            self.sessions.clear()
        for up in ups:
            up.stop()

    def status(self) -> dict:
        with self._lock:
            ups = list(self.upstreams.values())
            n = len(self.sessions)
        return {
            "pid": os.getpid(),
            "sessions": n,
            "upstreams": [u.status() for u in ups],
        }


def _safe_name(name: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in name).lstrip(".")
    return out or "server"


# --------------------------------------------------------------------------- #
# Streamable HTTP
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mindflock-mcp-bridge"
    hub: Hub
    token: str

    def log_message(self, fmt, *args):  # noqa: D401
        if _DEBUG:
            _log("http %s" % (fmt % args))

    # -- plumbing ----------------------------------------------------------- #
    def _authorized(self) -> bool:
        origin = self.headers.get("Origin")
        if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
            return False  # DNS-rebinding guard (the spec's MUST for local servers)
        auth = self.headers.get("Authorization") or ""
        want = "Bearer " + self.token
        return hmac.compare_digest(auth.encode(), want.encode())

    def _json(self, code: int, body: Any, headers: Optional[dict] = None) -> None:
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _start_sse(self, headers: Optional[dict] = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _chunk(self, text: str) -> None:
        data = text.encode("utf-8")
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def _sse(self, msg: dict) -> None:
        self._chunk(
            "event: message\ndata: %s\n\n" % json.dumps(msg, separators=(",", ":"))
        )

    def _end_sse(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass

    def _client_gone(self) -> bool:
        """Whether the client closed its end (EOF waiting on the socket) — so a
        window that went away stops counting as a live user within a second,
        not at the next keepalive write."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _server_name(self) -> Optional[str]:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.startswith("/mcp/"):
            name = path[len("/mcp/") :]
            return name if name and "/" not in name else None
        return None

    # -- verbs -------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if self.path.split("?", 1)[0] == "/health":
            return self._json(200, dict(self.hub.status(), ok=True))
        if self._server_name() is None:
            return self._json(404, {"error": "not found"})
        s = self.hub.session(self.headers.get("Mcp-Session-Id", ""))
        if s is None:
            return self._json(404, _error(None, INVALID_REQUEST, "unknown session"))
        if "text/event-stream" not in (self.headers.get("Accept") or ""):
            return self._json(405, None, {"Allow": "POST, DELETE"})
        q = s.listen()
        try:
            self._start_sse()
            next_ping = time.time() + KEEPALIVE_S
            while True:
                try:
                    msg = q.get(timeout=1.0)
                except queue.Empty:
                    if self.hub.session(s.sid) is None or self._client_gone():
                        break
                    if time.time() >= next_ping:
                        self._chunk(": keepalive\n\n")
                        next_ping = time.time() + KEEPALIVE_S
                    continue
                self._sse(msg)
        except OSError:
            pass  # the client went away
        finally:
            s.unlisten(q)
        self._end_sse()
        self.close_connection = True

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        ok = self.hub.close_session(self.headers.get("Mcp-Session-Id", ""))
        self._json(200 if ok else 404, None)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        name = self._server_name()
        if name is None:
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"null")
        except (ValueError, OSError):
            return self._json(400, _error(None, -32700, "parse error"))
        batch = body if isinstance(body, list) else [body]
        if not batch or not all(isinstance(m, dict) for m in batch):
            return self._json(400, _error(None, INVALID_REQUEST, "invalid request"))
        sse_ok = "text/event-stream" in (self.headers.get("Accept") or "")

        init = [m for m in batch if m.get("method") == "initialize"]
        if init:
            if len(batch) != 1:
                return self._json(
                    400, _error(None, INVALID_REQUEST, "initialize must be alone")
                )
            msg = init[0]
            try:
                s, result = self.hub.open_session(name, msg.get("params"))
            except KeyError:
                return self._json(
                    404,
                    _error(
                        msg.get("id"), INVALID_REQUEST, "no shared server %r" % name
                    ),
                )
            except RuntimeError as e:
                return self._json(200, _error(msg.get("id"), INTERNAL_ERROR, str(e)))
            return self._json(
                200, _result(msg.get("id"), result), {"Mcp-Session-Id": s.sid}
            )

        sid = self.headers.get("Mcp-Session-Id", "")
        if _DEBUG:
            _log(
                "post %s sid=%s %s" % (name, sid[:8], [m.get("method") for m in batch])
            )
        if not sid:
            # Before initialize — e.g. the ``server/discover`` probe a newer
            # client sends to detect a stateless server. Answer at once with
            # -32601 (it then falls back to initialize), as backend.mcp does.
            reqs = [m for m in batch if m.get("method") and m.get("id") is not None]
            if not reqs:
                return self._json(202, None)
            errs = [
                _error(m.get("id"), METHOD_NOT_FOUND, "initialize first") for m in reqs
            ]
            return self._json(200, errs if isinstance(body, list) else errs[0])
        s = self.hub.session(sid)
        if s is None or s.upstream.name != name:
            # Unknown (expired, or this bridge restarted): the spec's 404 tells
            # the client to start a new session with a fresh initialize.
            return self._json(404, _error(None, INVALID_REQUEST, "unknown session"))
        s.touch()
        up = s.upstream
        out: "queue.Queue" = queue.Queue()
        expected = 0
        pend: List[_Pending] = []
        for m in batch:
            method, mid = m.get("method"), m.get("id")
            if method is None:
                continue  # a response to a server request we never sent
            if mid is None:
                if method == "notifications/cancelled":
                    up.cancel(s, (m.get("params") or {}).get("requestId"))
                continue  # initialized / roots changes / anything else: swallowed
            expected += 1
            if method in ("ping", "logging/setLevel"):
                out.put(_result(mid, {}))
                continue
            try:
                if method == "resources/subscribe":
                    up.subscribe(s, m, out)
                elif method == "resources/unsubscribe":
                    up.unsubscribe(s, m, out)
                else:
                    p = up.forward(s, m, out)
                    if p is not None:
                        pend.append(p)
            except RuntimeError as e:
                out.put(_error(mid, INTERNAL_ERROR, str(e)))
        if not expected:
            return self._json(202, None)
        s.busy(1)
        try:
            if sse_ok:
                self._stream_replies(out, expected, pend, up)
            else:
                self._json_replies(out, expected, pend, up, isinstance(body, list))
        finally:
            s.busy(-1)

    def _stream_replies(self, out, expected: int, pend, up: Upstream) -> None:
        self._start_sse()
        got = 0
        deadline = time.time() + REQUEST_TIMEOUT_S
        next_ping = time.time() + KEEPALIVE_S
        try:
            while got < expected:
                try:
                    msg = out.get(timeout=1.0)
                except queue.Empty:
                    now = time.time()
                    if now > deadline or (
                        pend and all(p.done for p in pend) and out.empty()
                    ):
                        break  # timed out, or every forwarded request was cancelled
                    if now >= next_ping:  # also how a vanished client is noticed
                        self._chunk(": keepalive\n\n")
                        next_ping = now + KEEPALIVE_S
                    continue
                self._sse(msg)
                if "method" not in msg:
                    got += 1
        except OSError:
            for p in pend:
                up.drop_pending(p)
            self.close_connection = True
            return
        self._end_sse()

    def _json_replies(
        self, out, expected: int, pend, up: Upstream, as_list: bool
    ) -> None:
        replies: List[dict] = []
        deadline = time.time() + REQUEST_TIMEOUT_S
        while len(replies) < expected and time.time() < deadline:
            try:
                msg = out.get(timeout=1.0)
            except queue.Empty:
                if pend and all(p.done for p in pend) and out.empty():
                    break
                continue
            if "method" not in msg:
                replies.append(msg)  # progress is dropped without a stream
        if not replies:
            return self._json(202, None)
        self._json(200, replies if as_list else replies[0])


class _LoopbackHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` without the reverse-DNS lookup in
    ``server_bind``: the stock one sets ``server_name = socket.getfqdn(host)``,
    which on macOS can stall for many seconds — longer than the launch side
    waits for the bridge to come up (seen on the macOS CI runner: the bridge
    started, wrote nothing, and read as never started). The name is only
    cosmetic here; the bridge only ever serves the loopback address."""

    def server_bind(self) -> None:
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def make_server(
    hub: Hub, token: str, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    handler = type("BridgeHandler", (_Handler,), {"hub": hub, "token": token})
    srv = _LoopbackHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


# --------------------------------------------------------------------------- #
# Daemon entry point
# --------------------------------------------------------------------------- #
def _write_private_json(path: str, data: dict) -> None:
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backend.mcp.bridge")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--version", default="")
    ap.add_argument("--port", type=int, default=0, help="0 = any free port")
    args = ap.parse_args(argv)
    token = os.environ.pop(TOKEN_ENV, "")
    if not token:
        _log("no token in %s; refusing to start" % TOKEN_ENV)
        return 2
    state_dir = os.path.abspath(args.state_dir)
    os.makedirs(state_dir, mode=0o700, exist_ok=True)

    import fcntl  # POSIX only; the launch side never starts us elsewhere

    lock_fd = os.open(os.path.join(state_dir, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("another bridge holds the lock; exiting")
        return 3

    hub = Hub(state_dir)
    try:
        srv = make_server(hub, token, port=args.port)
    except OSError:
        srv = make_server(hub, token)  # the old port is taken: any free one
    port = srv.server_address[1]
    bridge_path = os.path.join(state_dir, BRIDGE_FILE)
    _write_private_json(
        bridge_path,
        {
            "pid": os.getpid(),
            "port": port,
            "token": token,
            "version": args.version,
            "started_at": time.time(),
        },
    )
    _log("listening on 127.0.0.1:%d" % port)

    stop = threading.Event()

    def _term(*_a):
        stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    threading.Thread(
        target=srv.serve_forever, name="mcp-bridge-http", daemon=True
    ).start()
    try:
        while not stop.wait(REAP_EVERY_S):
            hub.reap()
            if hub.idle_for() >= IDLE_EXIT_S:
                _log("idle for %ds; exiting" % IDLE_EXIT_S)
                break
    finally:
        try:
            with open(bridge_path, encoding="utf-8") as fh:
                if json.load(fh).get("pid") == os.getpid():
                    os.remove(bridge_path)
        except (OSError, ValueError):
            pass
        srv.shutdown()
        hub.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
