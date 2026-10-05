"""Attach the MindFlock MCP server to every session's agent CLI, per launch.

An agent that can see the rest of the flock — list sessions, read another
window's output, message a peer, spawn and steer workers — talks to the
MindFlock web server through the stdio MCP server in :mod:`backend.mcp`. This
module is the launch-side half: for each (re)launch it builds a
:class:`McpSpec` (which interpreter, which server, which session the agent IS)
and asks the session's provider for the CLI flags that attach it
(:meth:`~backend.providers.base.BaseProvider.mcp_launch_args`).

Per-launch FLAGS, never a merge into a CLI's own config file:

* Claude Code keys ``~/.claude.json`` project entries by the CANONICAL git root
  (verified live against 2.1.289), so a local-scope server written under a
  worktree's key never loads — and writing it under the repo key would leak the
  server into the user's own ``claude`` runs and make per-session cleanup
  impossible. Claude instead gets ``--mcp-config=<run file>``: a 0600 JSON file
  under :func:`run_dir`, rewritten on every launch, deleted by :func:`forget`.
* Codex gets ONE ``-c mcp_servers.mindflock={...}`` inline table, so an auth
  profile's ``CODEX_HOME`` (a per-account dir) is covered without touching any
  ``config.toml``.

The flags are prepended to the effective launch args at the launch sites
(:meth:`Instance._configure_launch_command`, the web relaunch in
``agent_sessions._ensure_agent_session``, the Assistant addon) — never inside
``build_launch_command`` / ``write_launcher`` themselves, so the golden launcher
scripts and exact-string launch tests are unaffected — and never persisted into
``Instance.LaunchArgs`` (they would show in the UI and go stale with the port).

Everything here is best-effort: :func:`attach_args` returns ``()`` on any
failure, and a session then simply launches without the MCP, exactly as before.

Every launch site also records whether THIS launch got the flags
(:func:`note_launch`), in memory, keyed by tmux session name: the session row's
``mcp_attached`` is what tells the UI that an agent started before the toggle
went on (or resumed by a path that bypasses the flags) has no MindFlock tools —
pasting a prompt that names them would only confuse it. Never persisted: after
a server restart a still-running agent was launched by another process, and
the honest answer is "unknown" (None).

Off switches: ``general.agent_mcp = false`` in settings, or
``MINDFLOCK_AGENT_MCP=0`` in the server's environment (wins over settings).
Live sessions pick a change up on their next (re)launch only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "SERVER_NAME",
    "MODULE",
    "AUTO_APPROVED_TOOLS",
    "SCOPES",
    "DEFAULT_SCOPE",
    "McpSpec",
    "enabled",
    "configured_scope",
    "server_port",
    "mcp_python",
    "mcp_pythonpath",
    "build_spec",
    "attach_args",
    "script_attached",
    "refresh_launcher_config",
    "note_launch",
    "launch_attached",
    "supported_providers",
    "run_dir",
    "run_file_path",
    "run_files",
    "claude_config",
    "write_claude_config",
    "claude_tool_names",
    "codex_server_table",
    "forget",
]

#: The server's name in every CLI's config. Claude Code names its tools
#: ``mcp__mindflock__<tool>`` from it — the spelling the MCP's own delivery and
#: report-back texts tell agents to call.
SERVER_NAME = "mindflock"

#: ``python -P -m backend.mcp``. ``-P`` (3.11+) keeps the agent's cwd off
#: ``sys.path``: an agent working ON MindFlock (a MindFlock worktree) would
#: otherwise import that branch's half-edited ``backend/`` package — or fail on
#: a branch that has no ``backend/mcp`` at all.
MODULE = "backend.mcp"
_PYTHON_ARGS = ("-P", "-m", MODULE)

#: Tools an agent may call without a permission prompt: reading the flock, its
#: own inbox, waiting, and reporting back to its OWN parent. A worker launched
#: without skip-permissions would otherwise block on its very first
#: report-back, which its parent can only observe as ``clarify``.
#:
#: ``send_message`` is deliberately NOT here: it can push text into ANY local
#: session, including one running with skip-permissions. Pre-approving it would
#: let a session whose every Bash/network call needs a human (say, one reading
#: a prompt-injected issue) steer a session that needs none — so in a gated
#: session it asks, like any other way of reaching outside the session.
#: Spawning, killing, re-parenting and answering another session's dialog keep
#: prompting too.
AUTO_APPROVED_TOOLS = (
    "whoami",
    "list_sessions",
    "get_session",
    "read_output",
    "get_diff",
    "check_inbox",
    "wait_for_message",
    "wait_for_session",
    "report_result",
)

#: Management scopes the MCP server understands, narrowest first; the attach
#: config hands the configured one to every session (``MINDFLOCK_MCP_SCOPE``).
SCOPES = ("readonly", "children", "all")
DEFAULT_SCOPE = "children"

#: The bind used when nothing else names the server's port (``run.py``'s own
#: default).
DEFAULT_PORT = 8765
_HOST = "127.0.0.1"

#: Client-side ceilings, a little above the MCP's longest wait (1500 s), so a
#: long ``wait_for_session`` returns its own timeout instead of being cut off.
#: Claude takes milliseconds in its per-server ``timeout`` (accepted in
#: ``--mcp-config`` files — verified against 2.1.289, which drops a server whose
#: ``timeout`` is not a number, so the key is schema-checked, not ignored);
#: Codex takes seconds.
TOOL_TIMEOUT_S = 1620
STARTUP_TIMEOUT_S = 30

#: Codex starts stdio servers with a CLEARED environment (only HOME, PATH, USER,
#: LANG, TERM, TMPDIR, … plus ``env``/``env_vars``), so the tmux identity
#: fallback and the auth token have to be forwarded by name.
_CODEX_ENV_VARS = ("TMUX", "TMUX_PANE", "TMUX_TMPDIR", "MINDFLOCK_AUTH_TOKEN")

_RUN_FILE_PREFIX = "mcp-"
_RUN_FILE_SUFFIX = ".json"
_FALSY = ("0", "false", "no", "off")


@dataclass(frozen=True)
class McpSpec:
    """Everything one launch needs to attach the MCP server.

    ``managed`` marks a session MindFlock launched for a known ``title``: the
    MCP then acts as that session and, if it cannot confirm who it is, fails
    CLOSED to read-only rather than becoming an external client with the run of
    the flock. The Assistant (not a session) is attached with ``managed=False``
    and no title, which the MCP treats as an external client. No auth token is
    ever baked in — the MCP reads it from the environment or the settings file.
    """

    title: str
    tmux_name: str
    workdir: str = ""
    python: str = ""
    pythonpath: str = ""
    host: str = _HOST
    port: int = DEFAULT_PORT
    scope: str = DEFAULT_SCOPE
    settings_file: str = ""
    managed: bool = True

    def command(self) -> str:
        """The interpreter that runs the MCP server (absolute path)."""
        return self.python or sys.executable

    def args(self) -> tuple:
        """argv after :meth:`command`."""
        return _PYTHON_ARGS

    def env(self) -> dict:
        """The env vars the MCP server process is given (all strings)."""
        env = {
            "MINDFLOCK_HOST": self.host,
            "MINDFLOCK_PORT": str(int(self.port)),
            "MINDFLOCK_MCP_SCOPE": self.scope,
        }
        if self.managed:
            env["MINDFLOCK_MCP_MANAGED"] = "1"
            if self.title:
                env["MINDFLOCK_SESSION_TITLE"] = self.title
        if self.pythonpath:
            env["PYTHONPATH"] = self.pythonpath
        if self.settings_file:
            env["MINDFLOCK_SETTINGS_FILE"] = self.settings_file
        return env


# --------------------------------------------------------------------------- #
# Settings / environment resolution
# --------------------------------------------------------------------------- #
def _general_settings():
    """``general`` read FRESH from the settings file, or None when unreadable.

    Not through ``load_settings()``: its cache is per-process, and the ticket
    pipeline child (which launches sessions through the engine too) would
    otherwise keep launching with whatever the toggle was when it booted.
    Reading the file directly also leaves the server's own cache untouched.
    """
    try:
        from backend.config import settings as _s

        try:
            raw = json.loads(_s.settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        general = raw.get("general") if isinstance(raw, dict) else None
        return _s.GeneralSettings.from_dict(
            general if isinstance(general, dict) else {}
        )
    except Exception:  # noqa: BLE001 — a settings read must never block a launch
        return None


def enabled() -> bool:
    """Whether new launches attach the MCP server.

    ``MINDFLOCK_AGENT_MCP=0`` (or false/no/off) is a kill switch that wins over
    settings; otherwise ``general.agent_mcp`` decides, unset meaning ON.
    """
    if os.environ.get("MINDFLOCK_AGENT_MCP", "").strip().lower() in _FALSY:
        return False
    general = _general_settings()
    if general is None:
        return True
    return getattr(general, "agent_mcp", None) is not False


def configured_scope() -> str:
    """``general.agent_mcp_scope`` when it names a known scope, else
    :data:`DEFAULT_SCOPE`."""
    general = _general_settings()
    scope = str(getattr(general, "agent_mcp_scope", "") or "").strip().lower()
    return scope if scope in SCOPES else DEFAULT_SCOPE


def _valid_port(value: str) -> Optional[int]:
    value = (value or "").strip()
    if value.isdigit() and 0 < int(value) < 65536:
        return int(value)
    return None


def server_port() -> int:
    """The port the MindFlock web server listens on.

    ``MINDFLOCK_SERVER_PORT`` (what the server hands the ticket pipeline child),
    then ``UVICORN_PORT`` (``run.py`` always exports it), then a ``--port``
    argument, then :data:`DEFAULT_PORT`. Deliberately NEVER ``PORT``: inside an
    agent session that is the session's dev-port block
    (:mod:`backend.web.core.ports`), so a pipeline started from an agent shell
    would point every MCP at the session's own dev server.
    """
    for var in ("MINDFLOCK_SERVER_PORT", "UVICORN_PORT"):
        port = _valid_port(os.environ.get(var, ""))
        if port is not None:
            return port
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv):
            port = _valid_port(argv[i + 1])
        elif arg.startswith("--port="):
            port = _valid_port(arg.split("=", 1)[1])
        else:
            continue
        if port is not None:
            return port
    return DEFAULT_PORT


def mcp_python() -> str:
    """The interpreter for the MCP server: ``MINDFLOCK_MCP_PYTHON`` when set (the
    server passes its own to the pipeline child, which may run a different
    venv), else this process's."""
    return (os.environ.get("MINDFLOCK_MCP_PYTHON") or "").strip() or sys.executable


def mcp_pythonpath() -> str:
    """The directory holding the ``backend`` package the MCP should import.

    ``MINDFLOCK_MCP_PYTHONPATH`` when set (travels with ``MINDFLOCK_MCP_PYTHON``,
    so the pipeline child pairs the server's interpreter with the server's
    package), else the parent of this process's own ``backend/``. Pinned on
    PYTHONPATH because ``-P`` drops the cwd and a source checkout run without
    an install has no other way onto ``sys.path``.
    """
    env = (os.environ.get("MINDFLOCK_MCP_PYTHONPATH") or "").strip()
    if env:
        return env
    import backend

    return os.path.dirname(os.path.dirname(os.path.abspath(backend.__file__)))


def build_spec(
    title: str, tmux_name: str, workdir: str = "", managed: bool = True
) -> McpSpec:
    """The :class:`McpSpec` for one launch, resolved from this process's env and
    the settings file."""
    return McpSpec(
        title=title or "",
        tmux_name=tmux_name,
        workdir=workdir or "",
        python=mcp_python(),
        pythonpath=mcp_pythonpath(),
        host=_HOST,
        port=server_port(),
        scope=configured_scope(),
        settings_file=(os.environ.get("MINDFLOCK_SETTINGS_FILE") or "").strip(),
        managed=managed,
    )


# --------------------------------------------------------------------------- #
# Launch-site entry points
# --------------------------------------------------------------------------- #
def attach_args(
    provider, *, title: str, tmux_name: str, workdir: str = "", managed: bool = True
) -> tuple:
    """argv tokens that attach the MCP to this launch, or ``()``.

    ``()`` when the feature is off, the provider has no attach support, there is
    no tmux name to key the run file on, or anything at all goes wrong — a
    launch must never fail over its MCP attachment. The tokens are raw argv
    elements: every launch path shell-quotes launch args itself.
    """
    if not tmux_name:
        return ()
    try:
        if not enabled():
            return ()
        spec = build_spec(title, tmux_name, workdir, managed=managed)
        return tuple(str(a) for a in (provider.mcp_launch_args(spec) or ()))
    except Exception:  # noqa: BLE001 — attachment is best-effort
        return ()


_MCP_CONFIG_IN_SCRIPT = re.compile(r"--mcp-config=([^'\"\s]+)")


def _script_run_files(script: str) -> list:
    """Run-file paths a launcher script names in ``--mcp-config=``, limited to
    our own run directory (nothing else is ever written to)."""
    root = os.path.abspath(run_dir())
    out = []
    for path in _MCP_CONFIG_IN_SCRIPT.findall(script or ""):
        full = os.path.abspath(path)
        if os.path.dirname(full) == root and full not in out:
            out.append(full)
    return out


def script_attached(script: str, want) -> bool:
    """Whether a launcher ``script`` carries every attach token in ``want``
    (each shell-quoted, the way the launcher writer bakes launch args in).
    False for an empty ``want``: nothing to attach is not attached."""
    want = tuple(want or ())
    return bool(want) and all(shlex.quote(str(a)) in (script or "") for a in want)


def launcher_attach_stale(
    provider, *, title: str, tmux_name: str, workdir: str, script: str
) -> bool:
    """Whether a provisioned launcher's baked attach args differ from what a
    launch would get NOW — so the launcher must be rewritten before it runs.

    The launcher bakes the args in when written: a reopen under a renamed title
    (Codex's ``MINDFLOCK_SESSION_TITLE`` / Claude's run-file path name the OLD
    session), a toggle turned off (the kill switch), or a changed scope all
    leave it attaching the wrong thing. Also (re)writes the current run file,
    as :func:`attach_args` does."""
    want = attach_args(provider, title=title, tmux_name=tmux_name, workdir=workdir)
    if not want:
        return "--mcp-config=" in script or ("mcp_servers.%s=" % SERVER_NAME) in script
    return not script_attached(script, want)


def refresh_launcher_config(
    provider, *, title: str, tmux_name: str, workdir: str, launcher: str
) -> bool:
    """Keep a provisioned launcher's ``--mcp-config`` file valid on a relaunch.

    The provisioned launcher script bakes the attach args in when it is first
    written, and the web relaunch re-runs that script rather than rebuilding
    the command (:func:`launcher_attach_stale` decides when it must be
    rewritten first). So: rewrite the run file (current port / interpreter /
    scope). When attaching is now OFF, every run file the script names is
    overwritten with an empty server list — the kill switch must hold for a
    relaunch too, not just new launches. A named file that is MISSING (a
    launcher that could not be rewritten) gets the same empty list: Claude
    refuses to start at all on a missing ``--mcp-config`` file, which inside
    the launcher's restart loop reads as an agent dying every three seconds.

    Returns whether the relaunch WILL attach: the script carries exactly
    what a launch would get now (:func:`script_attached`) and the feature is
    on. False on any doubt — the launch-record reading of "no tools".
    """
    try:
        want = attach_args(provider, title=title, tmux_name=tmux_name, workdir=workdir)
        try:
            with open(launcher, encoding="utf-8", errors="replace") as fh:
                script = fh.read()
        except OSError:
            return False
        off = not enabled()
        for path in _script_run_files(script):
            if off or not os.path.exists(path):
                _write_json_0600(path, {"mcpServers": {}})
        return not off and script_attached(script, want)
    except Exception:  # noqa: BLE001 — a relaunch must never fail over this
        return False


def supported_providers() -> list:
    """Names of registered providers that can be auto-attached (they override
    :meth:`~backend.providers.base.BaseProvider.mcp_launch_args`)."""
    from backend import providers as _providers
    from .base import BaseProvider

    names: list = []
    for prov in _providers.all_providers():
        impl = getattr(type(prov), "mcp_launch_args", None)
        if impl is not None and impl is not BaseProvider.mcp_launch_args:
            if prov.name not in names:
                names.append(prov.name)
    return names


# --------------------------------------------------------------------------- #
# Which launch got the tools (the row's ``mcp_attached``)
# --------------------------------------------------------------------------- #
_LAUNCHED: dict = {}
_LAUNCHED_LOCK = threading.Lock()


def note_launch(tmux_name: str, attached: bool) -> None:
    """Record that ``tmux_name``'s agent was just (re)launched, with
    (``attached=True``) or without the attach flags. Every launch site calls
    it — the engine's first Start and Resume, the web relaunch, the
    Assistant — whatever the reason the flags were missing (feature off, a
    CLI with no attach support, a path that bypasses them)."""
    if not tmux_name:
        return
    with _LAUNCHED_LOCK:
        _LAUNCHED[tmux_name] = bool(attached)


def launch_attached(tmux_name: str) -> Optional[bool]:
    """Whether ``tmux_name``'s current agent launch got the MindFlock tools:
    True / False as recorded by :func:`note_launch`, None when no launch was
    recorded by this process (adopted from another server, or not launched
    since this one started)."""
    with _LAUNCHED_LOCK:
        return _LAUNCHED.get(tmux_name or "")


# --------------------------------------------------------------------------- #
# Run files (Claude --mcp-config)
# --------------------------------------------------------------------------- #
def run_dir() -> str:
    """Where per-session run files live: ``$MINDFLOCK_RUN_DIR``, else
    ``~/.mindflock/run``."""
    env = (os.environ.get("MINDFLOCK_RUN_DIR") or "").strip()
    if env:
        return env
    from backend.config.config import GetConfigDir

    return os.path.join(GetConfigDir(), "run")


def run_file_path(tmux_name: str) -> str:
    """``<run dir>/mcp-<tmux_name>.json``. The name is re-sanitized so no tmux
    name can escape the directory — and when that sanitizing is LOSSY (any
    non-ASCII or punctuation: "修复登录" and "添加测试", "a+b" and "a_b") a
    digest of the real name is appended, so two live sessions can never share
    one file (one's launch would hand the other its identity; one's removal
    would delete the other's config)."""
    raw = tmux_name or ""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", raw) or "session"
    safe = safe.lstrip(".") or "session"
    if raw and safe != raw:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        safe = "%s-%s" % (safe[:64], digest)
    return os.path.join(run_dir(), _RUN_FILE_PREFIX + safe + _RUN_FILE_SUFFIX)


def run_files() -> list:
    """Every MCP run file currently on disk (sorted), for uninstall."""
    root = run_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(
        os.path.join(root, n)
        for n in names
        if n.startswith(_RUN_FILE_PREFIX) and n.endswith(_RUN_FILE_SUFFIX)
    )


def claude_tool_names() -> tuple:
    """:data:`AUTO_APPROVED_TOOLS` as Claude Code permission-rule names."""
    return tuple("mcp__%s__%s" % (SERVER_NAME, t) for t in AUTO_APPROVED_TOOLS)


def claude_config(spec: McpSpec) -> dict:
    """The ``--mcp-config`` document for one session."""
    return {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": spec.command(),
                "args": list(spec.args()),
                "env": spec.env(),
                "timeout": TOOL_TIMEOUT_S * 1000,
            }
        }
    }


def _write_json_0600(path: str, data: dict) -> None:
    """Write ``data`` to ``path`` atomically, owner-only (dir 0700, file 0600).

    Same-directory temp file + ``os.replace``, so a CLI starting concurrently
    never reads a truncated file. Raises on failure."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass  # best-effort (a dir we don't own)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mcp.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_claude_config(spec: McpSpec) -> str:
    """(Re)write the session's Claude run file and return its path. Rewritten on
    every launch so a changed port, interpreter or scope is picked up."""
    path = run_file_path(spec.tmux_name)
    _write_json_0600(path, claude_config(spec))
    return path


def forget(tmux_name: str) -> bool:
    """Delete a session's run file (session deleted / closed) and its launch
    record. True when a file was removed; never raises."""
    if not tmux_name:
        return False
    with _LAUNCHED_LOCK:
        _LAUNCHED.pop(tmux_name, None)
    try:
        os.remove(run_file_path(tmux_name))
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001 — e.g. no resolvable home directory
        return False


# --------------------------------------------------------------------------- #
# Codex inline table
# --------------------------------------------------------------------------- #
_TOML_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_str(value: str) -> str:
    """``value`` as a TOML basic string.

    ``json.dumps`` almost works and is what this mirrors, but not quite: with
    ``ensure_ascii`` it escapes astral characters as UTF-16 surrogate pairs
    (``\\ud83d\\ude00``), which TOML rejects, and without it it leaves DEL raw,
    which TOML also rejects. Codex silently treats an unparseable ``-c`` value
    as a plain STRING, so a bad escape would not error — it would make the whole
    server entry fail to deserialize and Codex refuse to start.
    """
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif 0xD800 <= code <= 0xDFFF:
            out.append("�")  # a lone surrogate is not a Unicode scalar
        elif code < 0x20 or code == 0x7F:
            out.append("\\u%04x" % code)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_array(items) -> str:
    return "[" + ",".join(_toml_str(str(i)) for i in items) + "]"


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml_str(key)


def codex_server_table(spec: McpSpec) -> str:
    """The ``mcp_servers.mindflock`` value for Codex's ``-c``, as a one-line TOML
    inline table (round-trips through ``tomllib``).

    ``env`` values are strings (Codex deserializes it as a string map, so a bare
    port number would fail). The listed tools are pre-approved
    (``approval_mode = "approve"``) — the Codex twin of Claude's
    ``--allowedTools``.
    """
    env = ",".join(
        "%s=%s" % (_toml_key(k), _toml_str(v)) for k, v in sorted(spec.env().items())
    )
    tools = ",".join(
        '%s={approval_mode="approve"}' % _toml_key(t) for t in AUTO_APPROVED_TOOLS
    )
    return (
        "{command=%s,args=%s,env={%s},env_vars=%s,"
        "startup_timeout_sec=%d,tool_timeout_sec=%d,tools={%s}}"
        % (
            _toml_str(spec.command()),
            _toml_array(spec.args()),
            env,
            _toml_array(_CODEX_ENV_VARS),
            STARTUP_TIMEOUT_S,
            TOOL_TIMEOUT_S,
            tools,
        )
    )
