"""Cloudflare quick tunnel for the peer relay ingress.

``cloudflared tunnel --url http://127.0.0.1:<ingress port>`` (no account)
gives the ingress a public ``https://<random>.trycloudflare.com`` hostname,
which then goes into ``mfp2:`` invite codes. Cloudflare terminates the outer
TLS and forwards the WebSocket; the pinned peer TLS runs inside it, so
Cloudflare (or anyone who hijacks the hostname) can drop traffic but can't
read, alter or impersonate (docs/peer-link.md, "Connecting across networks").

Hardening:

* We never download anything. The binary is ``peer.cloudflared_path`` or
  ``cloudflared`` on ``PATH``; no binary, no tunnel (the caller says so).
* cloudflared runs with a minimal environment (no inherited ``TUNNEL_*`` /
  ``TUNNEL_TOKEN`` variables), ``HOME`` pointed at a private empty directory
  and an explicit ``--config`` file we write, so a user's own
  ``~/.cloudflared`` or ``/etc/cloudflared`` config (ingress rules, origin
  certs) can never widen what is exposed. ``--url`` names the ingress port,
  never the MindFlock web port.
* Its output is untrusted text: read with a per-line cap, scanned for one
  strictly shaped ``https://<label>.trycloudflare.com`` token (never a URL
  with a path, like the API endpoint in error messages), and never logged.
* :meth:`QuickTunnel.stop` terminates it (then kills it); MindFlock stops the
  tunnel as soon as no invite or listener link needs it.
* It never outlives the server: on Linux the child asks the kernel for
  ``SIGTERM`` when its parent dies (``PR_SET_PDEATHSIG``); everywhere, each
  running cloudflared is registered (:func:`pids_dir`: its pid, start time
  and exact command line, and the server's own pid and start time), and
  :func:`reap_stale` — run when a tunnel starts and at server start — ends
  one whose server is gone. It matches the recorded pid AND start time AND
  command line, never a pattern, and leaves alone a tunnel whose server
  still runs (two servers may share one state folder). A stale tunnel would
  otherwise keep a public hostname pointing at a freed loopback port.
* Why a start failed is classified against a fixed set of patterns
  (:func:`classify_failure`) into one of :data:`FAILURE_TEXT`'s reasons; the
  line itself never leaves this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys

from backend import compat, osflags
from backend.peer import paths

__all__ = [
    "QuickTunnel",
    "TunnelError",
    "FAILURE_TEXT",
    "classify_failure",
    "find_cloudflared",
    "parse_quick_tunnel_host",
    "QUICK_TUNNEL_SUFFIX",
]

log = logging.getLogger(__name__)

QUICK_TUNNEL_SUFFIX = "trycloudflare.com"
START_TIMEOUT = 45.0
REGISTER_GRACE = 20.0  # after the hostname appears, wait this long for the edge
MAX_LINE = 4096
STOP_TIMEOUT = 5.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# A bare origin standing alone (line start, whitespace or the banner's "|"
# on both sides): scheme, ONE DNS label, the quick-tunnel suffix — so
# "https://api.trycloudflare.com/tunnel" (an error message), a quoted URL,
# "https://x.trycloudflare.com.evil.example" or one buried in another URL
# never match.
_HOST_RE = re.compile(
    r"(?<![^\s|])https://([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"\.trycloudflare\.com(?=[\s|]|\Z)"
)
_RESERVED_LABELS = frozenset({"api", "www"})
_REGISTERED_RE = re.compile(r"\bRegistered tunnel connection\b")


#: Why a quick tunnel didn't come up, in words a person can act on. The keys
#: are all :attr:`TunnelError.reason` can be; nothing from cloudflared's own
#: output is ever shown.
FAILURE_TEXT = {
    "rate_limited": "Cloudflare is rate-limiting quick tunnels right now",
    "unreachable": "this computer can't reach Cloudflare (a firewall or proxy?)",
    "failed": "the Cloudflare tunnel didn't start",
}

# Fixed patterns over cloudflared's (untrusted) output; first match wins.
_FAILURE_PATTERNS = (
    ("rate_limited", re.compile(r"\b429\b|too many requests|rate.?limit", re.I)),
    (
        "unreachable",
        re.compile(
            r"i/o timeout|no such host|connection refused|network is unreachable"
            r"|failed to (?:dial|request quick tunnel)|context deadline exceeded"
            r"|tls handshake",
            re.I,
        ),
    ),
)


def classify_failure(line) -> str | None:
    """The :data:`FAILURE_TEXT` reason one output line points at, or None."""
    if not isinstance(line, str):
        return None
    line = line[:MAX_LINE]
    for reason, rx in _FAILURE_PATTERNS:
        if rx.search(line):
            return reason
    return None


class TunnelError(Exception):
    """``reason`` is a :data:`FAILURE_TEXT` key."""

    def __init__(self, message: str = "", reason: str = "failed"):
        super().__init__(message)
        self.reason = reason if reason in FAILURE_TEXT else "failed"


def find_cloudflared(configured: str = "") -> str | None:
    """The cloudflared binary to run: ``configured`` (an absolute path to an
    executable file), else ``cloudflared`` on PATH. Never downloads."""
    configured = (configured or "").strip()
    if configured:
        path = os.path.expanduser(configured)
        if os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        return None
    return shutil.which("cloudflared")


def parse_quick_tunnel_host(line) -> str | None:
    """The quick-tunnel hostname announced on one line of cloudflared output,
    or None. Tolerates anything (ANSI codes, junk, huge lines)."""
    if not isinstance(line, str):
        return None
    line = _ANSI_RE.sub("", line[:MAX_LINE])
    m = _HOST_RE.search(line)
    if m is None or m.group(1) in _RESERVED_LABELS:
        return None
    host = f"{m.group(1)}.{QUICK_TUNNEL_SUFFIX}"
    return host if len(host) <= 253 else None


async def _lines(stream: asyncio.StreamReader):
    """Decoded lines, each cut to :data:`MAX_LINE`; overlong lines are split
    rather than buffered without bound."""
    while True:
        try:
            raw = await stream.readuntil(b"\n")
        except asyncio.IncompleteReadError as e:
            if e.partial:
                yield e.partial[:MAX_LINE].decode("utf-8", "replace")
            return
        except asyncio.LimitOverrunError as e:
            raw = await stream.readexactly(e.consumed)
        yield raw[:MAX_LINE].decode("utf-8", "replace")


def tunnel_dir() -> str:
    return os.path.join(paths.peer_root(), "relay")


# --------------------------------------------------------------------------- #
# Never outliving the server
# --------------------------------------------------------------------------- #
def pids_dir() -> str:
    """Where every running cloudflared of this state folder is registered
    (one JSON file each), whichever feature started it."""
    return os.path.join(paths.peer_root(), "cloudflared-pids")


def _proc_facts(pid: int):
    """``(create_time, cmdline)`` of a live process, or None (gone, or not
    ours to inspect). Never raises."""
    try:
        import psutil

        p = psutil.Process(int(pid))
        with p.oneshot():
            if p.status() == psutil.STATUS_ZOMBIE:
                return None
            return float(p.create_time()), list(p.cmdline())
    except Exception:  # noqa: BLE001 — NoSuchProcess, AccessDenied, …
        return None


def _same_start(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 0.5
    except (TypeError, ValueError):
        return False


def _register(pid: int, argv: list) -> str:
    """Record a just-started cloudflared → the record's path (``""`` when it
    couldn't be written; the tunnel still runs). The command line recorded
    is the one the OS reports for it (spawning returns after the exec), so
    the reaper compares like with like; ``argv`` is what we asked to run."""
    me = _proc_facts(os.getpid())
    child = _proc_facts(pid)
    if me is None or child is None or not child[1]:
        return ""
    if child[1][-len(argv) + 1 :] != [str(a) for a in argv][1:]:
        return ""  # not the process we just started (gone and pid reused?)
    doc = {
        "pid": int(pid),
        "started": child[0],
        "argv": child[1],
        "owner": os.getpid(),
        "owner_started": me[0],
    }
    try:
        d = paths.ensure_dir(pids_dir())
        path = os.path.join(d, "%d-%d.json" % (os.getpid(), int(pid)))
        tmp = path + ".tmp"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | osflags.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
        _MINE.add(path)
        return path
    except OSError:
        return ""


#: The records THIS process image wrote. A server that re-execs itself in
#: place (``core.restart``) keeps its pid and start time, so "its server
#: still runs" can't tell a tunnel the previous image left behind — this can.
_MINE: set = set()


def _unregister(path: str) -> None:
    if path:
        _MINE.discard(path)
        with contextlib.suppress(OSError):
            os.unlink(path)


def reap_stale(folder: str | None = None) -> int:
    """End every registered cloudflared whose server is gone → how many.

    A record is acted on only when its server (pid + start time) no longer
    runs — or is this very process, re-exec'd since it wrote the record —
    AND a process with the recorded pid, start time and exact command
    line still does — then that one process is terminated (killed if it
    lingers). A record whose process is gone, or whose pid now belongs to
    something else, is just dropped. Never raises."""
    folder = folder or pids_dir()
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return 0
    ended = 0
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            pid, owner = int(doc["pid"]), int(doc["owner"])
            argv = doc["argv"]
            if not (
                isinstance(argv, list)
                and argv
                and all(isinstance(a, str) for a in argv)
            ):
                raise ValueError("bad argv")
        except (OSError, ValueError, KeyError, TypeError):
            _unregister(path)
            continue
        if path in _MINE:
            continue  # this server's own, live
        owner_now = _proc_facts(owner)
        if (
            owner != os.getpid()
            and owner_now is not None
            and _same_start(owner_now[0], doc.get("owner_started"))
        ):
            continue  # another server still runs it (a shared state folder)
        child = _proc_facts(pid)
        if (
            child is None
            or not _same_start(child[0], doc.get("started"))
            or child[1] != argv
        ):
            _unregister(path)  # gone, or the pid is someone else's now
            continue
        try:
            import psutil

            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(STOP_TIMEOUT)
            except psutil.TimeoutExpired:
                p.kill()
                with contextlib.suppress(Exception):
                    p.wait(STOP_TIMEOUT)
            ended += 1
            log.warning("peer: ended a cloudflared left running by a stopped server")
        except Exception:  # noqa: BLE001 — gone meanwhile, or not ours
            pass
        _unregister(path)
    return ended


def _die_with_parent_kwargs() -> dict:
    """Linux: ``preexec_fn`` that makes the kernel SIGTERM the child when
    this server dies (``prctl(PR_SET_PDEATHSIG)``), exiting at once if the
    server is already gone. Elsewhere ``{}`` (the registry covers it)."""
    if not sys.platform.startswith("linux"):
        return {}
    try:
        import ctypes

        prctl = ctypes.CDLL(None, use_errno=True).prctl  # resolved pre-fork
    except Exception:  # noqa: BLE001
        return {}
    parent = os.getpid()
    sig = int(signal.SIGTERM)

    def _child() -> None:
        prctl(1, sig, 0, 0, 0)  # PR_SET_PDEATHSIG
        if os.getppid() != parent:
            os._exit(1)

    return {"preexec_fn": _child}


class QuickTunnel:
    """One ``cloudflared`` quick-tunnel process for ``origin_port``.

    ``spawn`` is ``asyncio.create_subprocess_exec``-compatible (tests pass a
    fake). ``on_exit()`` is called once if the process dies on its own."""

    def __init__(
        self,
        binary: str,
        origin_port: int,
        *,
        workdir: str | None = None,
        start_timeout: float = START_TIMEOUT,
        register_grace: float = REGISTER_GRACE,
        spawn=None,
        on_exit=None,
    ):
        if (
            isinstance(origin_port, bool)
            or not isinstance(origin_port, int)
            or not 0 < origin_port < 65536
        ):
            raise ValueError("bad origin port")
        self.binary = binary
        self.origin_port = origin_port
        self.workdir = workdir or tunnel_dir()
        self.start_timeout = start_timeout
        self.register_grace = register_grace
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._on_exit = on_exit
        self.proc = None
        self.hostname: str | None = None
        self.registered = False
        # The FAILURE_TEXT reason the output pointed at while starting.
        self.failure: str | None = None
        self._reader: asyncio.Task | None = None
        self._stopping = False
        self._pidfile = ""

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def argv(self, config_path: str) -> list[str]:
        return [
            self.binary,
            "tunnel",
            "--no-autoupdate",
            "--config",
            config_path,
            "--metrics",
            "127.0.0.1:0",
            "--url",
            f"http://127.0.0.1:{self.origin_port}",
        ]

    def _prepare(self) -> tuple[str, dict]:
        home = paths.ensure_dir(os.path.join(paths.ensure_dir(self.workdir), "home"))
        cfg = os.path.join(self.workdir, "cloudflared.yml")
        fd = os.open(
            cfg,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | osflags.O_NOFOLLOW
            | osflags.O_BINARY,
            0o600,
        )
        with os.fdopen(fd, "w") as f:
            f.write("# written by MindFlock: quick tunnel to the peer relay only\n")
            f.write("no-autoupdate: true\n")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": home,
            "LANG": "C",
        }
        if sys.platform == "win32":  # cloudflared needs these to start there
            for k in ("SYSTEMROOT", "TEMP", "TMP", "USERPROFILE"):
                if k in os.environ:
                    env[k] = os.environ[k]
        return cfg, env

    async def start(self) -> str:
        """Start cloudflared and return the public hostname once Cloudflare
        has issued it (and, best effort, registered a connection). Raises
        :class:`TunnelError`."""
        if self.alive and self.hostname:
            return self.hostname
        await self.stop()
        self._stopping = False
        self.failure = None
        cfg, env = self._prepare()
        await asyncio.to_thread(reap_stale)
        argv = self.argv(cfg)
        try:
            self.proc = await self._spawn(
                *argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=self.workdir,
                **compat.detached_kwargs(),
                **_die_with_parent_kwargs(),
                limit=MAX_LINE,
            )
        except OSError as e:
            raise TunnelError(
                f"could not start cloudflared: {type(e).__name__}"
            ) from None
        if getattr(self.proc, "pid", None):
            self._pidfile = await asyncio.to_thread(_register, self.proc.pid, argv)
        found: asyncio.Future = asyncio.get_running_loop().create_future()
        registered = asyncio.Event()
        self._reader = asyncio.create_task(
            self._read(self.proc, found, registered), name="peer-cloudflared"
        )
        try:
            async with asyncio.timeout(self.start_timeout):
                host = await found
        except TimeoutError:
            reason = self.failure or "unreachable"
            await self.stop()
            raise TunnelError(
                "cloudflared did not report a tunnel address", reason
            ) from None
        except TunnelError as e:
            reason = self.failure or e.reason
            await self.stop()
            raise TunnelError(str(e), reason) from None
        self.hostname = host
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self.register_grace):
                await registered.wait()
        # Deliberately no DNS check here: the new name may take a while to
        # appear, and trycloudflare.com caches NXDOMAIN for 60 s, so probing
        # early would only poison our resolver. The joiner waits instead
        # (PeerTransport.pair).
        log.info("peer: relay tunnel up at %s", host)
        return host

    async def _read(self, proc, found: asyncio.Future, registered: asyncio.Event):
        try:
            async for line in _lines(proc.stdout):
                if not found.done():
                    host = parse_quick_tunnel_host(line)
                    if host is not None:
                        found.set_result(host)
                    elif self.failure is None:
                        self.failure = classify_failure(_ANSI_RE.sub("", line))
                elif not registered.is_set() and _REGISTERED_RE.search(line):
                    registered.set()
                    self.registered = True
            await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("peer: cloudflared output reader stopped: %s", type(e).__name__)
        finally:
            if not found.done():
                found.set_exception(
                    TunnelError("cloudflared exited before the tunnel was up")
                )
            # (Retrieving the exception also keeps asyncio from warning.)
            was_up = not found.cancelled() and found.exception() is None
        if proc.returncode is not None and self.proc is proc:
            pidfile, self._pidfile = self._pidfile, ""
            _unregister(pidfile)
        if was_up and not self._stopping:
            log.warning("peer: relay tunnel (cloudflared) exited")
            self.hostname = None
            if self._on_exit is not None:
                with contextlib.suppress(Exception):
                    self._on_exit()

    async def stop(self) -> None:
        self._stopping = True
        proc, self.proc = self.proc, None
        self.hostname = None
        self.registered = False
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
        if proc is None or proc.returncode is not None:
            pidfile, self._pidfile = self._pidfile, ""
            _unregister(pidfile)
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader
