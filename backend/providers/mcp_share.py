"""Share stdio MCP servers across sessions — the launch side of the bridge.

Every agent window normally spawns its own copy of every stdio MCP server in
the user's config. For the servers the user marks shareable
(``general.mcp_shared_servers``) MindFlock instead runs ONE copy per machine in
the bridge daemon (:mod:`backend.mcp.bridge`) and points each launch at it over
streamable HTTP. CLI-neutral plumbing lives here: the setting, the bridge's
lifetime, the registry of definitions it serves, the endpoint a launch is
handed. Reading a CLI's own MCP config — and the override that swaps the
user's stdio entry for the bridge URL — belongs to the provider (Claude's is
:meth:`backend.providers.claude.ClaudeProvider.shared_mcp_entries`).

State lives in ``<run dir>/mcp-shared/`` (0700):

* ``endpoint.json`` — the port and bearer token every run file names, chosen
  once and REUSED: a restarted bridge binds the same port with the same token,
  and Claude Code (verified 2.1.299) notices the dropped notification stream,
  re-initializes and carries on — so a bridge restart never strands a window.
* ``bridge.json`` — written by the live bridge (pid, port, token, version).
* ``servers.json`` — the definitions it may serve (0600: they carry the
  servers' own tokens, exactly as ``~/.claude.json`` does).
* ``logs/`` — ``bridge.log`` and one ``<server>.log`` of stderr per upstream.

Lifetime: the first launch that needs the bridge starts it, detached in its own
session (``start_new_session``) so it survives a MindFlock server restart the
way the tmux windows using it do; the next server finds it through
``bridge.json``. It stops each upstream once that server has had no client for
a grace period and exits itself when idle, so nothing outlives its users for
long — and ``mindflock uninstall`` stops it (:func:`stop_bridge`).

Everything is best-effort: any failure returns nothing, and the launch simply
keeps the user's own stdio entry (one copy per window, as before).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import time
import urllib.error
import urllib.request
from typing import Optional

from backend.fsutil import atomic_write_json

__all__ = [
    "STATE_SUBDIR",
    "enabled",
    "shared_names",
    "state_dir",
    "expand_definition",
    "share_caution",
    "ensure_bridge",
    "publish",
    "endpoint_entry",
    "bridge_status",
    "bridge_pid",
    "stop_bridge",
]

STATE_SUBDIR = "mcp-shared"
_ENDPOINT_FILE = "endpoint.json"
_START_LOCK = "start.lock"

#: How long a launch waits for a freshly started bridge to listen.
START_TIMEOUT_S = 8.0
HEALTH_TIMEOUT_S = 1.5

_FALSY = ("0", "false", "no", "off")

#: A server name the user may share. MindFlock's own server is never one: it
#: carries the session's identity.
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
RESERVED = ("mindflock",)


# --------------------------------------------------------------------------- #
# The setting
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    """``MINDFLOCK_MCP_SHARE=0`` turns sharing off whatever the setting says;
    otherwise it is on wherever :func:`shared_names` names something. POSIX
    only (the bridge needs ``fcntl`` and process groups)."""
    if os.name != "posix":
        return False
    return os.environ.get("MINDFLOCK_MCP_SHARE", "").strip().lower() not in _FALSY


def clean_names(value) -> list:
    """``value`` as a de-duplicated list of shareable server names (order kept;
    reserved and malformed names dropped)."""
    out: list = []
    if not isinstance(value, (list, tuple)):
        return out
    for v in value:
        name = str(v or "").strip()
        if _NAME_RE.match(name) and name not in RESERVED and name not in out:
            out.append(name)
    return out


def shared_names() -> list:
    """``general.mcp_shared_servers`` read FRESH from the settings file (the
    ticket pipeline child launches sessions too, and its settings cache is its
    own), ``[]`` when sharing is off or unreadable."""
    if not enabled():
        return []
    try:
        from backend.config import settings as _s

        raw = json.loads(_s.settings_path().read_text(encoding="utf-8"))
        general = raw.get("general") if isinstance(raw, dict) else None
        value = general.get("mcp_shared_servers") if isinstance(general, dict) else None
        return clean_names(value)
    except Exception:  # noqa: BLE001 — a settings read must never block a launch
        return []


# --------------------------------------------------------------------------- #
# Definitions
# --------------------------------------------------------------------------- #
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: str, env) -> Optional[str]:
    """``${VAR}`` / ``${VAR:-default}`` expanded from ``env`` (Claude Code's
    syntax), or None when a variable without a default is unset — a server
    whose definition depends on the SESSION's environment cannot be shared."""
    missing = []

    def sub(m):
        if m.group(1) in env:
            return env[m.group(1)]
        if m.group(2) is not None:
            return m.group(2)
        missing.append(m.group(1))
        return ""

    out = _VAR_RE.sub(sub, value)
    return None if missing else out


def expand_definition(definition: dict, env=None) -> Optional[dict]:
    """A user's stdio server entry as the bridge runs it — ``command``,
    ``args``, ``env`` with variables expanded from ``env`` (default: this
    process's) — or None when it is not a shareable stdio entry."""
    env = os.environ if env is None else env
    if not isinstance(definition, dict):
        return None
    kind = str(definition.get("type") or "stdio").strip().lower()
    command = definition.get("command")
    if kind != "stdio" or not isinstance(command, str) or not command.strip():
        return None
    args = definition.get("args") or []
    if not isinstance(args, list):
        return None
    srv_env = definition.get("env") or {}
    if not isinstance(srv_env, dict):
        return None
    out_cmd = _expand(command, env)
    out_args = [_expand(str(a), env) for a in args]
    out_env = {str(k): _expand(str(v), env) for k, v in srv_env.items()}
    if out_cmd is None or None in out_args or None in out_env.values():
        return None
    return {"command": out_cmd, "args": out_args, "env": out_env}


#: Command/argument fragments of servers that read the window's folder or hold
#: per-client state. Sharing them is the user's call, but the UI says why not.
_CWD_HINTS = (
    "gitnexus",
    "serena",
    "server-filesystem",
    "mcp-server-git",
    "server-git",
    "filesystem",
)
_STATE_HINTS = (
    "postgres",
    "mysql",
    "sqlite",
    "playwright",
    "puppeteer",
    "browser",
    "excalidraw",
    "memory",
)


def share_caution(name: str, definition: dict) -> str:
    """A one-line reason this server may NOT behave when shared, or ``""``.

    A heuristic for the Settings row — the user still decides."""
    text = " ".join(
        [name, str(definition.get("command") or "")]
        + [str(a) for a in definition.get("args") or []]
    ).lower()
    if any(h in text for h in _CWD_HINTS):
        return "Works on the folder it starts in — a shared copy starts in your home folder, not each session's."
    if any(h in text for h in _STATE_HINTS):
        return "Keeps state per client (connections, pages, a canvas) — sessions would share it."
    args = [str(a) for a in definition.get("args") or []]
    if any(a in (".", "./") or a.startswith("./") for a in args):
        return "Has a relative path argument — a shared copy resolves it from your home folder."
    return ""


# --------------------------------------------------------------------------- #
# Bridge lifetime
# --------------------------------------------------------------------------- #
def state_dir() -> str:
    from . import mcp_attach

    return os.path.join(mcp_attach.run_dir(), STATE_SUBDIR)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_private(path: str, data: dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    atomic_write_json(path, data, sort_keys=True, mode=0o600, prefix=".share.")


def _endpoint(d: str) -> dict:
    """``{port, token}`` every run file names — created once, then reused so a
    restarted bridge answers at the same URL with the same token."""
    path = os.path.join(d, _ENDPOINT_FILE)
    ep = _read_json(path)
    if isinstance(ep.get("port"), int) and ep.get("token"):
        return ep
    ep = {"port": _free_port(), "token": secrets.token_urlsafe(32)}
    _write_private(path, ep)
    return ep


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _health(port: int, token: str, timeout: float = HEALTH_TIMEOUT_S) -> Optional[dict]:
    req = urllib.request.Request(
        "http://127.0.0.1:%d/health" % port,
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(
            req, timeout=timeout
        ) as resp:  # noqa: S310 — loopback
            data = json.loads(resp.read() or b"{}")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) and data.get("ok") else None


def _live_bridge(d: str) -> Optional[dict]:
    info = _read_json(os.path.join(d, "bridge.json"))
    port, token = info.get("port"), info.get("token")
    if not (_pid_alive(info.get("pid")) and isinstance(port, int) and token):
        return None
    if _health(port, str(token)) is None:
        return None
    return info


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("mindflock")
    except Exception:  # noqa: BLE001
        return ""


#: Bridges this process started: polled so an exited one is reaped, not left
#: a zombie that :func:`_pid_alive` would still count.
_SPAWNED: list = []


def _reap_spawned() -> None:
    for proc in list(_SPAWNED):
        if proc.poll() is not None:
            _SPAWNED.remove(proc)


def ensure_bridge() -> Optional[dict]:
    """The running bridge's ``{pid, port, token}``, starting it if need be;
    None on any failure (the caller then leaves the user's entries alone)."""
    if os.name != "posix":
        return None
    _reap_spawned()
    try:
        import fcntl

        d = state_dir()
        live = _live_bridge(d)
        if live is not None:
            return live
        os.makedirs(d, mode=0o700, exist_ok=True)
        lock_fd = os.open(os.path.join(d, _START_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)  # one starter at a time
            live = _live_bridge(d)
            if live is not None:
                return live
            return _start(d)
        finally:
            os.close(lock_fd)
    except Exception:  # noqa: BLE001 — sharing is best-effort
        return None


def _start(d: str) -> Optional[dict]:
    from . import mcp_attach

    ep = _endpoint(d)
    logs = os.path.join(d, "logs")
    os.makedirs(logs, mode=0o700, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = mcp_attach.mcp_pythonpath()
    env["MINDFLOCK_MCP_BRIDGE_TOKEN"] = str(ep["token"])
    for var in ("PORT", "MINDFLOCK_AUTH_TOKEN"):  # nothing of the server's own
        env.pop(var, None)
    try:
        os.remove(os.path.join(d, "bridge.json"))
    except OSError:
        pass
    logf = open(os.path.join(logs, "bridge.log"), "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [
                mcp_attach.mcp_python(),
                "-P",
                "-m",
                "backend.mcp.bridge",
                "--state-dir",
                d,
                "--port",
                str(int(ep["port"])),
                "--version",
                _version(),
            ],
            cwd=os.path.expanduser("~"),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,  # outlives a server restart; own group
            close_fds=True,
        )
    finally:
        logf.close()
    _SPAWNED.append(proc)
    deadline = time.time() + START_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            # Lost a race to another bridge (it holds the lock), or crashed.
            return _live_bridge(d)
        info = _read_json(os.path.join(d, "bridge.json"))
        if info.get("pid") == proc.pid and _health(
            int(info["port"]), str(info["token"])
        ):
            if info["port"] != ep["port"]:  # the old port was taken
                _write_private(
                    os.path.join(d, _ENDPOINT_FILE), dict(ep, port=info["port"])
                )
            return info
        time.sleep(0.1)
    return _live_bridge(d)


def publish(definitions: dict) -> None:
    """Make ``{name: definition}`` (as :func:`expand_definition` returns them)
    the set the bridge may serve. Raises on failure."""
    path = os.path.join(state_dir(), "servers.json")
    if _read_json(path) != definitions:
        _write_private(path, definitions)


def endpoint_entry(bridge: dict, name: str, timeout_ms: Optional[int] = None) -> dict:
    """The Claude-style ``mcpServers`` entry pointing ``name`` at the bridge."""
    entry = {
        "type": "http",
        "url": "http://127.0.0.1:%d/mcp/%s" % (int(bridge["port"]), name),
        "headers": {"Authorization": "Bearer %s" % bridge["token"]},
    }
    if timeout_ms:
        entry["timeout"] = int(timeout_ms)
    return entry


def bridge_status() -> dict:
    """What Settings shows: is the bridge up, which upstreams run (pid, how
    many sessions use each, refused server→client requests). Never raises."""
    _reap_spawned()
    try:
        d = state_dir()
        info = _read_json(os.path.join(d, "bridge.json"))
        port, token = info.get("port"), info.get("token")
        if not (_pid_alive(info.get("pid")) and isinstance(port, int) and token):
            return {"running": False, "upstreams": []}
        health = _health(port, str(token)) or {}
        return {
            "running": bool(health),
            "pid": info.get("pid"),
            "port": port,
            "sessions": health.get("sessions", 0),
            "upstreams": [
                {
                    k: u.get(k)
                    for k in (
                        "name",
                        "pid",
                        "running",
                        "sessions",
                        "starts",
                        "refused_requests",
                    )
                }
                for u in health.get("upstreams") or []
            ],
            "log_dir": os.path.join(d, "logs"),
        }
    except Exception:  # noqa: BLE001
        return {"running": False, "upstreams": []}


def bridge_pid() -> Optional[int]:
    """The live bridge's pid, or None. Never raises."""
    try:
        pid = _read_json(os.path.join(state_dir(), "bridge.json")).get("pid")
        return pid if _pid_alive(pid) else None
    except Exception:  # noqa: BLE001
        return None


def stop_bridge(timeout: float = 5.0) -> bool:
    """SIGTERM the bridge (it stops its upstreams on the way out). True when
    one was running. Never raises."""
    try:
        info = _read_json(os.path.join(state_dir(), "bridge.json"))
        pid = info.get("pid")
        if not _pid_alive(pid):
            return False
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        if _pid_alive(pid):
            os.killpg(pid, signal.SIGKILL)
        _reap_spawned()
        return True
    except Exception:  # noqa: BLE001
        return False
