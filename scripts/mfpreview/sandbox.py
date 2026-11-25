"""The preview's isolation: one scratch MindFlock that cannot reach the
machine it runs on.

What "isolated" means here, and how each piece is enforced:

* **Its own home.** ``$HOME``, ``$MINDFLOCK_HOME``, every ``MINDFLOCK_*``
  store override, ``XDG_*``, ``TMPDIR`` and the git config all point inside a
  fresh temp dir (``/tmp/mfpv-…`` by default). The server is started with an
  ALLOWLISTED environment — nothing of the caller's leaks through (no
  ``CLAUDE_CONFIG_DIR``, ``GH_TOKEN``, API keys, ``CS_WEB_MODE``, ``PORT``…)
  — and with ``MINDFLOCK_HOME_GUARD=1``, so a store that still resolves into
  the real ``~/.mindflock`` raises instead of writing
  (:mod:`backend.config.home_guard`). :func:`assert_isolated` refuses to boot
  when a path in that env points outside the scratch dir, or the scratch dir
  lies inside a real home.
* **Its own tmux server.** The engine runs bare ``tmux``; a ``tmux`` wrapper
  first on ``PATH`` unsets ``$TMUX`` and pins EVERY call (the server's, a
  hook's, the stub agent's) to ``-S <scratch>/tmux/sock``. Teardown kills
  that server by that socket only — never the default one.
* **A spare port.** Chosen by the OS, never one of :data:`RESERVED_PORTS`
  (the owner's dev :9000, the installed :8765); bound to 127.0.0.1 only.
* **Nothing machine-global.** No Cursor-window adoption, no ``claude agents
  --json``, no systemd slice, no ticket pipeline, no update check, no voice
  warm-up, no PATH enrichment from the login shell. ``PATH`` is the scratch
  ``bin`` plus the system dirs only (no ``~/.local/bin``, no ``/mnt/c``);
  ``tailscale``, ``cloudflared``, ``gh``, ``systemctl``/``systemd-run``,
  editors and Windows interop are stubs that fail.
* **No real agent.** ``claude`` is :mod:`mfpreview.stub_agent`.
* **One at a time, and cleaned up.** A ``flock`` in the run dir holds while
  a preview lives; a state file records its pids, socket and scratch dir so
  the next ``up`` / ``down`` cleans up after a crash. Every process the
  preview started carries ``MFPREVIEW_ID=<id>`` in its environment, which is
  how the final sweep finds strays.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Ports a preview never takes: the owner's dev server and the installed app.
RESERVED_PORTS = frozenset({8765, 9000})

#: The only directories on the server's PATH besides the scratch ``bin``.
SYSTEM_PATH = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)

#: Caller variables that may pass through (locale and terminal only).
PASSTHROUGH_ENV = ("LANG", "LANGUAGE", "TZ", "USER", "LOGNAME")

#: Machine-global tools the server could reach for: each becomes a stub that
#: fails, so nothing reaches the tailnet, GitHub, systemd, an editor or Windows.
FAILING_STUBS = (
    "tailscale",
    "tailscale.exe",
    "cloudflared",
    "gh",
    "systemctl",
    "systemd-run",
    "code",
    "cursor",
    "xdg-open",
    "wslview",
    "explorer.exe",
    "powershell.exe",
    "pwsh.exe",
    "cmd.exe",
    "wsl.exe",
    "wt.exe",
)

#: The scratch server's settings: onboarded, no update check, no Cursor
#: adoption, no ticket pipeline.
BASE_SETTINGS: Dict[str, Any] = {
    "general": {"onboarded": True, "ingestion_autostart": False},
    "updates": {"mode": "off"},
    "ui": {"cursor_autoadopt": False},
}

#: Switches that keep the server's background loops off the machine.
SWITCHES = {
    "MINDFLOCK_AUTH": "0",
    "CS_WEB_MODE": "local",
    "CS_CURSOR_AUTOADOPT": "0",
    "CS_WEB_OPEN": "0",
    "MINDFLOCK_REVIVE_ON_START": "0",
    "MINDFLOCK_AGENT_SCOPE": "0",
    "MINDFLOCK_DISABLE_AGENTS_JSON": "1",
    "MINDFLOCK_VOICE_WARM": "0",
    "MINDFLOCK_HOST_MEMORY": "0",
    "MINDFLOCK_CODE_MAP_WARM": "0",
    "MINDFLOCK_NO_PATH_ENRICH": "1",
    "MINDFLOCK_SLEEP": "0",
    "MINDFLOCK_AUTO_CLOSE": "0",
    "MINDFLOCK_MODEL_DOWNSHIFT": "0",
    "MINDFLOCK_WORKTREE_GC": "0",
    "MINDFLOCK_AGENT_MCP": "0",
    "MINDFLOCK_MCP_SHARE": "0",
    "MINDFLOCK_MEMORY_GUARD": "0",
    "MINDFLOCK_SHED_IDLE": "0",
    "MINDFLOCK_HOME_GUARD": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "TERM": "xterm-256color",
}

#: Env keys whose values are paths that must stay inside the scratch dir.
PATH_KEYS = (
    "HOME",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "MINDFLOCK_HOME",
    "MINDFLOCK_SETTINGS_FILE",
    "MINDFLOCK_ASSISTANT_DIR",
    "MINDFLOCK_ACTIVITY_MARKER_DIR",
    "MINDFLOCK_RUN_DIR",
    "MINDFLOCK_FLIGHTS_DIR",
    "MINDFLOCK_REPO_ROOT",
    "MINDFLOCK_CONFIG",
    "MINDFLOCK_EXTENSIONS_DIR",
    "MINDFLOCK_HOOKS_DIR",
    "MINDFLOCK_PEER_HOME",
    "MINDFLOCK_CLAUDE_JSON",
    "MINDFLOCK_TAILSCALE_STATUS_FILE",
    "MINDFLOCK_TAILSCALE_BIN",
    "MINDFLOCK_CLOUDFLARED",
    "GIT_CONFIG_GLOBAL",
    "TMUX_TMPDIR",
)


class PreviewError(RuntimeError):
    """A refusal or a failed start, in words for the person running it."""


# --------------------------------------------------------------------------- #
# Where things live
# --------------------------------------------------------------------------- #
def real_homes() -> List[str]:
    """Every directory that counts as the real user's home: the password
    database's (what ``$HOME`` cannot fake) and the caller's ``$HOME``."""
    homes = []
    try:
        import pwd

        homes.append(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
    except Exception:  # noqa: BLE001
        pass
    env_home = os.environ.get("HOME")
    if env_home:
        homes.append(os.path.realpath(env_home))
    out: List[str] = []
    for h in homes:
        if h and h != os.sep and h not in out:
            out.append(h)
    return out


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def default_run_dir() -> Path:
    """Where the lock + state file live: ``$MFPREVIEW_RUN_DIR``, else
    ``$XDG_RUNTIME_DIR/mindflock-preview``, else ``/tmp/mindflock-preview-<uid>``."""
    override = os.environ.get("MFPREVIEW_RUN_DIR", "").strip()
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg and os.path.isdir(xdg) and not any(_inside(xdg, h) for h in real_homes()):
        return Path(xdg) / "mindflock-preview"
    return Path(tempfile.gettempdir()) / ("mindflock-preview-%d" % os.getuid())


def real_tmux() -> str:
    """The system tmux binary (never a wrapper inside a scratch dir)."""
    found = shutil.which("tmux", path=os.pathsep.join(SYSTEM_PATH))
    if not found:
        raise PreviewError(
            "tmux is not installed (looked in %s)" % ":".join(SYSTEM_PATH)
        )
    return os.path.realpath(found)


def pick_port(requested: int = 0) -> int:
    """A free 127.0.0.1 port that is not reserved. ``requested`` is honoured
    only when it is neither reserved nor in use."""
    if requested:
        if requested in RESERVED_PORTS:
            raise PreviewError(
                "port %d is reserved (the real MindFlock servers use %s); "
                "omit --port for a free one" % (requested, sorted(RESERVED_PORTS))
            )
        if port_in_use(requested):
            raise PreviewError("port %d is already in use" % requested)
        return requested
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port not in RESERVED_PORTS:
            return port
    raise PreviewError("could not find a free port")


def port_in_use(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Scripts written into the scratch bin
# --------------------------------------------------------------------------- #
TMUX_WRAPPER = r"""#!/bin/sh
# MindFlock preview: every tmux call goes to the preview's own server.
# $TMUX (set inside any tmux pane, including the caller's) would otherwise
# win over TMUX_TMPDIR and reach the real server, so it is dropped, and any
# -S / -L the caller passed is replaced by the preview socket.
unset TMUX
n=$#
i=0
skip=0
global=1
while [ "$i" -lt "$n" ]; do
  a=$1
  shift
  i=$((i + 1))
  if [ "$skip" = 1 ]; then skip=0; continue; fi
  if [ "$global" = 1 ]; then
    case "$a" in
      -S|-L) skip=1; continue ;;
      -S*|-L*) continue ;;
      -f|-c|-T) set -- "$@" "$a"; if [ "$i" -lt "$n" ]; then a=$1; shift; i=$((i + 1)); set -- "$@" "$a"; fi; continue ;;
      -*) ;;
      *) global=0 ;;
    esac
  fi
  set -- "$@" "$a"
done
exec '{real}' -S '{sock}' "$@"
"""

FAILING_STUB = """#!/bin/sh
echo "{name}: not available in the MindFlock preview sandbox" >&2
exit 1
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


# --------------------------------------------------------------------------- #
# The sandbox
# --------------------------------------------------------------------------- #
class Sandbox:
    """One preview: the scratch layout, the server process, the teardown.

    ``scratch_parent`` is where the ``mfpv-…`` dir is made (default: the
    system temp dir); ``run_dir`` holds the lock and the state file."""

    def __init__(
        self,
        scenario: str = "sky",
        scratch_parent: Optional[str] = None,
        run_dir: Optional[Path] = None,
        port: int = 0,
        onboarded: bool = True,
        keep: bool = False,
    ) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.scenario = scenario
        self.onboarded = onboarded
        self.keep = keep
        self.run_dir = Path(run_dir) if run_dir else default_run_dir()
        parent = scratch_parent or tempfile.gettempdir()
        for h in real_homes():
            if _inside(parent, h):
                raise PreviewError(
                    "refusing a scratch dir inside the real home %s (%s): the "
                    "preview keeps everything outside it" % (h, parent)
                )
        self.scratch_parent = Path(parent)
        self.requested_port = port
        self.port = 0
        self.scratch: Path = Path()
        self.proc: Optional[subprocess.Popen] = None
        self.env: Dict[str, str] = {}
        self._lock_fd: Optional[int] = None
        self._log = None
        self.real_tmux = real_tmux()

    # -- paths ----------------------------------------------------------------
    @property
    def home(self) -> Path:
        return self.scratch / "home"

    @property
    def mf_home(self) -> Path:
        return self.home / ".mindflock"

    @property
    def settings_path(self) -> Path:
        return self.mf_home / "settings.json"

    @property
    def bin_dir(self) -> Path:
        return self.scratch / "bin"

    @property
    def repos_dir(self) -> Path:
        return self.scratch / "repos"

    @property
    def roles_path(self) -> Path:
        return self.scratch / "roles.json"

    @property
    def tmux_socket(self) -> Path:
        return self.scratch / "tmux" / "sock"

    @property
    def marker_dir(self) -> Path:
        return self.home / ".mindflock-assistant" / ".activity-markers"

    @property
    def log_path(self) -> Path:
        return self.scratch / "server.log"

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"

    # -- lock -----------------------------------------------------------------
    def acquire(self) -> None:
        """Take the one-preview lock (cleaning up after a crashed preview
        first); :class:`PreviewError` when another preview is alive."""
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.run_dir / "lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            os.close(fd)
            if err.errno in (errno.EAGAIN, errno.EACCES):
                st = read_state(self.run_dir) or {}
                raise PreviewError(
                    "a preview is already running (scenario %r at %s); stop it "
                    "with `scripts/preview.py down` — one preview at a time"
                    % (st.get("scenario"), st.get("url"))
                )
            raise
        self._lock_fd = fd
        stale = read_state(self.run_dir)
        if stale:
            cleanup_stale(stale, self.real_tmux)
            try:
                self.state_path.unlink()
            except OSError:
                pass

    def release(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def write_state(self, state: str, **extra: Any) -> None:
        data = {
            "id": self.id,
            "state": state,
            "scenario": self.scenario,
            "url": self.base_url if self.port else "",
            "port": self.port,
            "supervisor_pid": os.getpid(),
            "server_pid": self.proc.pid if self.proc else None,
            "scratch": str(self.scratch),
            "tmux_socket": str(self.tmux_socket),
            "log": str(self.log_path),
            "updated": time.time(),
        }
        data.update(extra)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.state_path)

    # -- layout ---------------------------------------------------------------
    def lay_out(self) -> None:
        self.scratch = Path(
            tempfile.mkdtemp(prefix="mfpv-", dir=str(self.scratch_parent))
        )
        if len(str(self.tmux_socket)) > 100:
            raise PreviewError(
                "scratch path too long for a tmux socket (%s); use a shorter "
                "--scratch-parent" % self.tmux_socket
            )
        for d in (
            self.mf_home,
            self.home / ".mindflock-assistant",
            self.marker_dir,
            self.bin_dir,
            self.repos_dir,
            self.scratch / "tmp",
            self.scratch / "run",
            self.scratch / "pipeline",
            self.scratch / "extensions",
            self.scratch / "no-hooks",
            self.scratch / "cwd",
            self.tmux_socket.parent,
            self.home / ".config",
            self.home / ".local" / "share",
            self.home / ".local" / "state",
            self.home / ".cache",
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.tmux_socket.parent.chmod(0o700)
        settings = json.loads(json.dumps(BASE_SETTINGS))
        settings["general"]["onboarded"] = bool(self.onboarded)
        self.settings_path.write_text(json.dumps(settings, indent=2))
        (self.scratch / "config.toml").write_text("")
        # Login evidence for the stub ``claude`` (what the cold-install CI writes).
        (self.home / ".claude.json").write_text('{"oauthAccount": {"preview": true}}\n')
        (self.scratch / "gitconfig").write_text(
            "[user]\n\tname = Preview Bot\n\temail = preview@example.invalid\n"
            "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n"
        )
        self.roles_path.write_text("{}")
        # tmux's default status line names the machine; keep it out of
        # screenshots that may end up in a public PR.
        (self.home / ".tmux.conf").write_text("set -g status-right ' %H:%M '\n")
        _write_exec(
            self.bin_dir / "tmux",
            TMUX_WRAPPER.replace("{real}", self.real_tmux).replace(
                "{sock}", str(self.tmux_socket)
            ),
        )
        for name in FAILING_STUBS:
            _write_exec(self.bin_dir / name, FAILING_STUB.format(name=name))
        from mfpreview import stub_agent

        _write_exec(
            self.bin_dir / "claude",
            stub_agent.render(
                python=sys.executable,
                roles=str(self.roles_path),
                markers=str(self.marker_dir),
                tmux=str(self.bin_dir / "tmux"),
                gitconfig=str(self.scratch / "gitconfig"),
            ),
        )

    def build_env(self) -> Dict[str, str]:
        env = {k: os.environ[k] for k in PASSTHROUGH_ENV if os.environ.get(k)}
        for k, v in os.environ.items():
            if k.startswith("LC_"):
                env[k] = v
        env.update(SWITCHES)
        s = self.scratch
        env.update(
            {
                "MFPREVIEW_ID": self.id,
                "HOME": str(self.home),
                "SHELL": "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh",
                "PATH": os.pathsep.join([str(self.bin_dir), *SYSTEM_PATH]),
                "PORT": str(self.port),
                "TMPDIR": str(s / "tmp"),
                "XDG_RUNTIME_DIR": str(s / "run"),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_DATA_HOME": str(self.home / ".local" / "share"),
                "XDG_STATE_HOME": str(self.home / ".local" / "state"),
                "XDG_CACHE_HOME": str(self.home / ".cache"),
                "TMUX_TMPDIR": str(self.tmux_socket.parent),
                "MINDFLOCK_HOME": str(self.mf_home),
                "MINDFLOCK_SETTINGS_FILE": str(self.settings_path),
                "MINDFLOCK_ASSISTANT_DIR": str(self.home / ".mindflock-assistant"),
                "MINDFLOCK_ACTIVITY_MARKER_DIR": str(self.marker_dir),
                "MINDFLOCK_RUN_DIR": str(self.mf_home / "run"),
                "MINDFLOCK_FLIGHTS_DIR": str(self.mf_home / "flights"),
                "MINDFLOCK_REPO_ROOT": str(s / "pipeline"),
                "MINDFLOCK_CONFIG": str(s / "config.toml"),
                "MINDFLOCK_EXTENSIONS_DIR": str(s / "extensions"),
                "MINDFLOCK_HOOKS_DIR": str(s / "no-hooks"),
                "MINDFLOCK_PEER_HOME": str(self.home / ".mindflock-peer"),
                "MINDFLOCK_CLAUDE_JSON": str(self.home / ".claude.json"),
                "MINDFLOCK_TAILSCALE_STATUS_FILE": str(s / "no-tailnet.json"),
                "MINDFLOCK_TAILSCALE_BIN": str(self.bin_dir / "tailscale"),
                "MINDFLOCK_CLOUDFLARED": str(self.bin_dir / "cloudflared"),
                "GIT_CONFIG_GLOBAL": str(s / "gitconfig"),
                "PYTHONPATH": str(REPO_ROOT),
            }
        )
        self.env = env
        return env

    # -- lifecycle --------------------------------------------------------------
    def start(self, timeout: float = 90.0) -> None:
        """Lock, lay out, check, boot. On any failure everything is undone."""
        self.acquire()
        try:
            self.port = pick_port(self.requested_port)
            self.lay_out()
            self.build_env()
            assert_isolated(self.env, self.scratch, self.port)
            self.write_state("starting")
            self._boot(timeout)
            self.write_state("booted")
        except BaseException:
            self.stop()
            raise

    def _boot(self, timeout: float) -> None:
        self._log = open(self.log_path, "w")  # noqa: SIM115 — lives with the process
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "backend.web.run", "local"],
            cwd=str(self.scratch / "cwd"),
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=_die_with_parent,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise PreviewError(
                    "the preview server exited during boot (code %s):\n%s"
                    % (self.proc.returncode, self.log_tail())
                )
            try:
                hello = http_json(self.base_url + "/api/remote/hello", timeout=2)
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
                continue
            if isinstance(hello, dict):
                break
        else:
            raise PreviewError(
                "the preview server did not answer within %ss:\n%s"
                % (timeout, self.log_tail())
            )
        # The process answering must be OURS (run.py exits 0 "already
        # running" when the port holds another MindFlock).
        time.sleep(0.5)
        if self.proc.poll() is not None:
            raise PreviewError("another server answered on port %d" % self.port)
        adopt = http_json(self.base_url + "/api/cursor/autoadopt")
        if not isinstance(adopt, dict) or adopt.get("enabled") is not False:
            raise PreviewError("Cursor-window adoption is not off: %r" % (adopt,))

    def stop(self) -> Dict[str, Any]:
        """Tear down everything this preview started; returns what was done.
        Safe to call twice and after a partial start. A second Ctrl-C (uv
        forwards the terminal's, so one press can arrive twice) or a SIGTERM
        while it runs is ignored: an interrupted teardown is what leaves a
        server behind."""
        held = _hold_signals()
        try:
            return self._stop()
        finally:
            _restore_signals(held)

    def _stop(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {}
        proc, self.proc = self.proc, None
        if proc is not None:
            report["server"] = _stop_group(proc.pid, proc)
        if self.scratch and str(self.scratch) not in ("", "."):
            report["tmux"] = kill_tmux(self.real_tmux, self.tmux_socket)
        if self.id:
            report["strays"] = sweep(self.id)
        if self._log is not None:
            try:
                self._log.close()
            except Exception:  # noqa: BLE001
                pass
            self._log = None
        if (
            self.scratch
            and self.scratch.name.startswith("mfpv-")
            and self.scratch.exists()
        ):
            if self.keep:
                report["scratch"] = "kept: %s" % self.scratch
            else:
                shutil.rmtree(self.scratch, ignore_errors=True)
                report["scratch"] = "removed" if not self.scratch.exists() else "LEFT"
        if self._lock_fd is not None:
            try:
                self.state_path.unlink()
            except OSError:
                pass
        self.release()
        return report

    # -- helpers ----------------------------------------------------------------
    def log_tail(self, n: int = 4000) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-n:]
        except OSError:
            return ""

    def set_roles(self, roles: Dict[str, Dict[str, Any]]) -> None:
        cur = {}
        try:
            cur = json.loads(self.roles_path.read_text())
        except (OSError, ValueError):
            pass
        cur.update(roles)
        tmp = self.roles_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=2))
        os.replace(tmp, self.roles_path)

    def git(self, repo: Path, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            env=self.env or self.build_env(),
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def assert_isolated(env: Dict[str, str], scratch: Path, port: int) -> None:
    """Refuse to boot unless the server's env is sealed inside ``scratch``."""
    problems = []
    homes = real_homes()
    for h in homes:
        if _inside(str(scratch), h):
            problems.append("scratch %s is inside the real home %s" % (scratch, h))
    for key in PATH_KEYS:
        val = env.get(key)
        if not val:
            problems.append("%s is not set" % key)
        elif not _inside(val, str(scratch)):
            problems.append("%s=%s is outside the scratch dir" % (key, val))
    for key in (
        "TMUX",
        "TMUX_PANE",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "GH_CONFIG_DIR",
    ):
        if key in env:
            problems.append("%s must not reach the preview server" % key)
    for d in env.get("PATH", "").split(os.pathsep):
        if d and not (_inside(d, str(scratch)) or d in SYSTEM_PATH):
            problems.append("PATH entry %s is neither scratch nor a system dir" % d)
    if port in RESERVED_PORTS or not port:
        problems.append("port %r is reserved" % port)
    if env.get("PORT") != str(port):
        problems.append("PORT=%r is not the chosen port %d" % (env.get("PORT"), port))
    if env.get("MINDFLOCK_HOME_GUARD") != "1":
        problems.append("MINDFLOCK_HOME_GUARD is not on")
    if problems:
        raise PreviewError("refusing to start the preview:\n  " + "\n  ".join(problems))


# --------------------------------------------------------------------------- #
# Teardown pieces (also used for a crashed preview's leftovers)
# --------------------------------------------------------------------------- #
_HELD = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _hold_signals() -> Dict[int, Any]:
    import threading

    if threading.current_thread() is not threading.main_thread():
        return {}
    held = {}
    for sig in _HELD:
        try:
            held[sig] = signal.signal(sig, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
    return held


def _restore_signals(held: Dict[int, Any]) -> None:
    for sig, handler in held.items():
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError, TypeError):
            pass


def _die_with_parent() -> None:  # pragma: no cover — runs in the child
    """The server gets SIGTERM if the preview process dies (even SIGKILL)."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_group(pgid: int, proc: Optional[subprocess.Popen] = None) -> str:
    """SIGTERM the server's process group, then SIGKILL after 10 s."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "already gone"
    except PermissionError:
        return "not ours"
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc is not None:
            if proc.poll() is not None:
                break
        elif not _alive(pgid):
            break
        time.sleep(0.1)
    else:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return "killed"
    # Stragglers in the group (a launch helper) go too.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    return "stopped"


def kill_tmux(real: str, sock: Path) -> str:
    """Kill the preview's tmux server — by its socket, never the default."""
    sock = Path(sock)
    if not sock.exists():
        return "no server"
    env = {"PATH": os.pathsep.join(SYSTEM_PATH), "HOME": str(sock.parent)}
    subprocess.run(
        [real, "-S", str(sock), "kill-server"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    deadline = time.time() + 3
    while sock.exists() and time.time() < deadline:
        time.sleep(0.1)
    if sock.exists():
        try:
            sock.unlink()
        except OSError:
            pass
    return "killed"


def preview_pids(preview_id: str, exclude: Iterable[int] = ()) -> List[int]:
    """Live processes of this user whose environment carries
    ``MFPREVIEW_ID=<preview_id>`` — everything the preview started."""
    want = ("MFPREVIEW_ID=%s" % preview_id).encode()
    skip = {os.getpid(), *exclude}
    out = []
    uid = os.getuid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in skip:
            continue
        try:
            if os.stat("/proc/%d" % pid).st_uid != uid:
                continue
            with open("/proc/%d/environ" % pid, "rb") as f:
                envs = f.read().split(b"\0")
        except OSError:
            continue
        if want in envs:
            out.append(pid)
    return out


def sweep(preview_id: str) -> int:
    """SIGKILL every process still tagged with the preview id."""
    if not os.path.isdir("/proc"):
        return 0
    pids = preview_pids(preview_id)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return len(pids)


def read_state(run_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads((Path(run_dir) / "state.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lock_held(run_dir: Path) -> bool:
    path = Path(run_dir) / "lock"
    if not path.exists():
        return False
    fd = os.open(str(path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def cleanup_stale(state: Dict[str, Any], real: str) -> Dict[str, Any]:
    """Undo what a crashed preview left (its lock is free, its state file is
    not): tagged processes, its tmux server, its scratch dir."""
    report: Dict[str, Any] = {}
    pid = state.get("server_pid")
    pid_id = state.get("id") or ""
    if isinstance(pid, int) and pid_id and pid in preview_pids(pid_id):
        report["server"] = _stop_group(pid)
    sock = state.get("tmux_socket") or ""
    scratch = state.get("scratch") or ""
    if sock and scratch and _inside(sock, scratch):
        report["tmux"] = kill_tmux(real, Path(sock))
    if pid_id:
        report["strays"] = sweep(pid_id)
    p = Path(scratch) if scratch else None
    if (
        p
        and p.name.startswith("mfpv-")
        and p.is_dir()
        and not any(_inside(str(p), h) for h in real_homes())
    ):
        shutil.rmtree(p, ignore_errors=True)
        report["scratch"] = "removed"
    return report


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_json(url: str, timeout: float = 10.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def call(
    base_url: str, method: str, path: str, body: Any = None, timeout: float = 30.0
):
    """One API call → ``(status, parsed body)``; a 4xx/5xx is returned."""
    req = urllib.request.Request(
        base_url + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as err:
        raw, status = err.read(), err.code
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {"raw": raw.decode("utf-8", "replace")}
