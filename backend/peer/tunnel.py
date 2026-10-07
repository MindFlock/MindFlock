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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys

from backend.peer import paths

__all__ = [
    "QuickTunnel",
    "TunnelError",
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


class TunnelError(Exception):
    pass


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
        self._reader: asyncio.Task | None = None
        self._stopping = False

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
        fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
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
        cfg, env = self._prepare()
        try:
            self.proc = await self._spawn(
                *self.argv(cfg),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=self.workdir,
                start_new_session=True,
                limit=MAX_LINE,
            )
        except OSError as e:
            raise TunnelError(
                f"could not start cloudflared: {type(e).__name__}"
            ) from None
        found: asyncio.Future = asyncio.get_running_loop().create_future()
        registered = asyncio.Event()
        self._reader = asyncio.create_task(
            self._read(self.proc, found, registered), name="peer-cloudflared"
        )
        try:
            async with asyncio.timeout(self.start_timeout):
                host = await found
        except TimeoutError:
            await self.stop()
            raise TunnelError("cloudflared did not report a tunnel address") from None
        except TunnelError:
            await self.stop()
            raise
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
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader
