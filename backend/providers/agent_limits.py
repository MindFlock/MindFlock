"""Memory limits for the agents MindFlock launches, and a gate on GitNexus.

What ran this machine out of memory (2026-11-04, ``journalctl -k -b -1``):
20 GB of RAM and all 16 GB of swap held by ~18 agent trees, until the kernel
OOM killer fired — minutes into a swap thrash that left WSL wedged. The single
biggest processes were ``gitnexus analyze --force`` runs from a repo's
pre-commit hook (3.8 / 4.3 / 4.6 GB each, several at once: every agent that
committed started its own, and GitNexus sizes its heap to 0.75x RAM — 16 GB
here), then ``pytest -n 8`` suites (8 workers x ~0.7 GB) in other sessions.
The server (``oom_score_adj`` 0) was not the victim; the WSL VM was, because
nothing stopped one agent's tree from taking everything first.

Two guards, both applied where an agent is launched:

* **A cgroup per agent** (Linux with a systemd user manager). The launch runs
  inside ``systemd-run --user --scope`` with ``MemoryHigh`` (the kernel
  throttles and reclaims above it), ``MemoryMax`` (the kernel's OOM killer
  acts *inside that agent's tree* above it — the biggest process there, the
  test worker or the indexer, not the server or another agent) and
  ``MemorySwapMax``. Every agent scope sits in one slice,
  :data:`FLEET_SLICE`, which carries a limit for ALL agents together, so the
  server, the tmux server and the rest of the machine always keep the
  remainder. Where there is no systemd user manager (macOS, a WSL without
  systemd, a container) the launch is unchanged.

* **A ``gitnexus`` shim** on the agents' ``PATH`` (when GitNexus is
  installed). ``gitnexus analyze`` runs through ``mindflock slot --pool
  gitnexus --max 1`` (:mod:`backend.slot`): one analyze at a time machine-wide,
  queued first-come first-served, with a V8 heap cap (``NODE_OPTIONS
  --max-old-space-size``, which GitNexus honours instead of its 0.75x RAM
  default) and ``oom_score_adj`` 800, so an indexer is what the OOM killer
  takes first. Every other ``gitnexus`` subcommand (``mcp``, ``augment``…)
  execs the real binary untouched. It covers any repo's hook — a pre-commit
  ``gitnexus analyze``, ``node .gitnexus/run.cjs analyze`` (which looks
  ``gitnexus`` up on ``PATH``) — and an agent typing the command itself.

Settings (``general.agent_memory`` in settings.json, per device; every key
optional, ``""`` = the default)::

    {"enabled": true,                       # false = no scopes, no shim
     "high": "30%", "max": "40%", "swap": "15%",           # each agent
     "fleet_high": "65%", "fleet_max": "75%", "fleet_swap": "40%",  # all agents
     "gitnexus_heap_mb": 4096,              # 0 = no gitnexus shim
     "gitnexus_at_once": 1,
     "shed_idle": true}                     # core.memory_shield: sleep idle
                                            # agents when memory stays critical

``fleet_high`` is the share of RAM all agents together may use before the
kernel throttles and reclaims them collectively (``MemoryHigh`` on
:data:`FLEET_SLICE`): ten agents each under their own cap still swap the
box, the slice stops that first. The slice's limits are put on at server
start (:func:`ensure_fleet_slice`) and again whenever the scope probe
answers yes (every 10 minutes at most), so a user manager restarted since
gets them back; ``set-property --runtime`` is idempotent.

Sizes are a share of physical RAM (``"30%"``), bytes with an optional
K/M/G/T suffix (``"6G"``), or ``"off"`` for no limit. ``MINDFLOCK_AGENT_SCOPE=0``
turns the scopes off and ``MINDFLOCK_GITNEXUS_GATE=0`` the shim (the test suite
sets both).

Like :mod:`backend.providers.launch_script`, this depends on nothing outside
:mod:`backend.providers` but the settings file, so every launch path can use it.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from backend import compat

from .launch_script import shell_path

__all__ = [
    "DEFAULTS",
    "FLEET_SLICE",
    "ensure_fleet_slice",
    "SHIM_MARKER",
    "config",
    "gitnexus_shim_dir",
    "gitnexus_shim_text",
    "parse_size",
    "resolved_limits",
    "scope_available",
    "scope_command",
    "wrap_launch",
    "bash_path",
    "windows_scope_command",
]

#: The slice every agent scope goes in (a child of the user manager's
#: ``app.slice``); its own limits cap all agents together.
FLEET_SLICE = "app-mindflock.slice"

DEFAULTS: Dict[str, object] = {
    "enabled": True,
    # One agent: throttled above 30% of RAM, OOM-killed (inside its own tree)
    # above 40%, at most 15% of RAM worth of swap. A ~0.45 GB CLI with its MCP
    # servers plus a 4-worker test run fits under the throttle; an 8-worker
    # suite of 0.7 GB workers on a 20 GB machine meets the ceiling — and loses
    # a worker, not the machine.
    "high": "30%",
    "max": "40%",
    "swap": "15%",
    # All agents together: whatever they do, the server (~0.5-1.5 GB), the
    # tmux server, the editor and the OS keep a quarter of RAM and most swap.
    "fleet_high": "65%",
    "fleet_max": "75%",
    "fleet_swap": "40%",
    # 4 GB of V8 heap: the sitecheck repo's analyze peaked at 3.8-4.6 GB RSS
    # (heap + native graph store); one at a time, that fits anywhere this runs.
    "gitnexus_heap_mb": 4096,
    "gitnexus_at_once": 1,
    # core.memory_shield: when memory stays critical, put idle agents to sleep.
    "shed_idle": True,
}

_SIZE_KEYS = ("high", "max", "swap", "fleet_high", "fleet_max", "fleet_swap")
_INT_KEYS = ("gitnexus_heap_mb", "gitnexus_at_once")

#: First line after the shebang of a shim this module wrote: never resolved as
#: "the real gitnexus" and safe to overwrite.
SHIM_MARKER = "# mindflock gitnexus shim"

#: ``oom_score_adj`` an indexer gives itself (agents run at 500).
GITNEXUS_OOM_SCORE_ADJ = 800

#: Longest an analyze waits for its turn before running anyway, seconds. A
#: queue of agents committing one after another is minutes; a hung holder is
#: the only thing this ever trips on.
GITNEXUS_WAIT_SECONDS = 1800

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", re.I)
_PCT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")
_OFF_WORDS = ("off", "none", "infinity", "unlimited", "no", "false")
_UNIT_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


# --- settings ---------------------------------------------------------------


def _settings_blob() -> dict:
    """``general.agent_memory`` from settings.json, ``{}`` on any trouble."""
    try:
        from backend.config.settings import load_settings

        raw = load_settings().general.agent_memory
    except Exception:  # noqa: BLE001 — a launch never fails over a setting
        return {}
    return raw if isinstance(raw, dict) else {}


def clean_config(raw) -> Dict[str, object]:
    """The known keys of a stored ``agent_memory`` dict, junk dropped (what
    settings.json keeps — the defaults are never written)."""
    out: Dict[str, object] = {}
    if not isinstance(raw, dict):
        return out
    for key in ("enabled", "shed_idle"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    for key in _SIZE_KEYS:
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = str(int(v))
        if isinstance(v, str) and v.strip() and _valid_size(v):
            out[key] = v.strip()
    for key in _INT_KEYS:
        v = raw.get(key)
        if isinstance(v, bool):
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n >= 0:
            out[key] = n
    return out


def config(raw: Optional[dict] = None) -> Dict[str, object]:
    """:data:`DEFAULTS` overlaid with the stored settings (``raw`` for tests)."""
    merged = dict(DEFAULTS)
    merged.update(clean_config(_settings_blob() if raw is None else raw))
    return merged


def _valid_size(spec: str) -> bool:
    s = spec.strip().lower()
    return bool(s in _OFF_WORDS or _PCT_RE.match(s) or _SIZE_RE.match(s))


def _total_memory() -> int:
    if os.name == "nt":  # no sysconf there
        try:
            import psutil

            return int(psutil.virtual_memory().total)
        except Exception:  # noqa: BLE001
            return 0
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 0


def parse_size(spec, total: int) -> Optional[int]:
    """Bytes for a size setting: ``"30%"`` of ``total``, ``"6G"``, ``"512M"``,
    a plain byte count; ``0`` = no limit (``"off"``, ``"0"``); ``None`` for
    something unreadable (or a percentage with ``total`` unknown)."""
    s = str(spec if spec is not None else "").strip().lower()
    if not s:
        return None
    if s in _OFF_WORDS:
        return 0
    m = _PCT_RE.match(s)
    if m:
        if total <= 0:
            return None
        pct = float(m.group(1))
        return 0 if pct <= 0 else int(total * min(pct, 100.0) / 100.0)
    m = _SIZE_RE.match(s)
    if not m:
        return None
    mult = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}
    return int(float(m.group(1)) * mult[m.group(2).lower()])


def resolved_limits(
    cfg: Optional[Dict[str, object]] = None, total: Optional[int] = None
) -> Dict[str, int]:
    """Every size setting in bytes (``0`` = unlimited), defaults filling in
    anything unreadable."""
    cfg = config() if cfg is None else cfg
    total = _total_memory() if total is None else total
    out: Dict[str, int] = {}
    for key in _SIZE_KEYS:
        value = parse_size(cfg.get(key), total)
        if value is None:
            value = parse_size(DEFAULTS[key], total)
        out[key] = value or 0
    return out


def _env_off(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("0", "off", "false", "no")


# --- the systemd user scope ---------------------------------------------------

_PROBE_LOCK = threading.Lock()
#: (checked at monotonic, available). A yes is trusted for 10 minutes, a no for
#: one: a user manager that was not up yet at boot (WSL starts it lazily) is
#: picked up by the next launch a minute later.
_PROBE: Optional[Tuple[float, bool]] = None
_PROBE_TTL_YES = 600.0
_PROBE_TTL_NO = 60.0


def _systemd_run() -> Optional[str]:
    if not sys.platform.startswith("linux"):
        return None
    return shutil.which("systemd-run")


def _memory_props(prefix: str, limits: Dict[str, int]) -> List[str]:
    """``MemoryHigh=…`` style assignments for the non-zero limits."""
    props = []
    for key, prop in (
        ("high", "MemoryHigh"),
        ("max", "MemoryMax"),
        ("swap", "MemorySwapMax"),
    ):
        value = limits.get(prefix + key, 0)
        if value > 0:
            props.append("%s=%d" % (prop, value))
    return props


def _apply_fleet_limits(limits: Dict[str, int]) -> bool:
    """Put the all-agents limits on :data:`FLEET_SLICE` (runtime-only: gone at
    reboot, re-applied at the next server start or scope probe). Idempotent
    (``set-property`` of the same values is a no-op). → whether systemctl
    took them. Best-effort."""
    props = _memory_props("fleet_", limits)
    systemctl = shutil.which("systemctl")
    if not props or not systemctl:
        return False
    try:
        return (
            subprocess.run(
                [systemctl, "--user", "set-property", "--runtime", FLEET_SLICE, *props],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_fleet_slice() -> bool:
    """Server start: put the all-agents limits on :data:`FLEET_SLICE` now,
    before any agent launches (the probe would otherwise only do it at the
    first launch). Linux with a reachable systemd user manager only; a no-op
    (False) wherever scopes are off or unavailable, exactly as
    :func:`wrap_launch` is. Blocking (up to two short systemctl calls):
    off the event loop. Never raises."""
    try:
        if not config().get("enabled", True):
            return False
        if not scope_available():
            return False
        return _apply_fleet_limits(resolved_limits())
    except Exception:  # noqa: BLE001 — a guard never blocks startup
        return False


def scope_available(*, refresh: bool = False) -> bool:
    """Whether launches can run in a systemd user scope: ``systemd-run`` exists
    and a throwaway scope in :data:`FLEET_SLICE` actually starts (the user
    manager is up and reachable). The first yes also puts the fleet limits on
    the slice. Cached (see :data:`_PROBE`)."""
    global _PROBE
    if _env_off("MINDFLOCK_AGENT_SCOPE"):
        return False
    with _PROBE_LOCK:
        now = time.monotonic()
        if not refresh and _PROBE is not None:
            at, ok = _PROBE
            if now - at < (_PROBE_TTL_YES if ok else _PROBE_TTL_NO):
                return ok
        exe = _systemd_run()
        ok = False
        if exe:
            try:
                ok = (
                    subprocess.run(
                        [
                            exe,
                            "--user",
                            "--scope",
                            "--quiet",
                            "--collect",
                            "--slice=" + FLEET_SLICE,
                            "--",
                            "true",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    ).returncode
                    == 0
                )
            except (OSError, subprocess.SubprocessError):
                ok = False
        _PROBE = (now, ok)
    if ok:
        # Every fresh yes (at most one per _PROBE_TTL_YES): a user manager
        # restarted since the last one lost the runtime properties.
        _apply_fleet_limits(resolved_limits())
    return ok


def unit_name(session_name: str) -> str:
    """A unique, readable scope name for one launch of ``session_name``."""
    safe = _UNIT_SAFE.sub("_", session_name or "agent")[:80] or "agent"
    return "mindflock-%s-%d" % (safe, time.time_ns() // 1_000_000)


def scope_command(
    session_name: str,
    limits: Optional[Dict[str, int]] = None,
    exe: str = "systemd-run",
) -> List[str]:
    """The ``systemd-run`` argv (up to and including ``--``) that starts one
    agent launch in its own scope with the per-agent limits.

    ``OOMPolicy=continue``: when the OOM killer takes ONE process of the
    scope (a pytest worker or a build the agent ran hitting ``MemoryMax``),
    only that process dies. The default (``DefaultOOMPolicy=stop``) stops the
    whole unit, so the agent itself went down with exit 143 — a session gone
    offline "for no reason" over one child's allocation."""
    limits = resolved_limits() if limits is None else limits
    argv = [
        exe,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--slice=" + FLEET_SLICE,
        "--unit=" + unit_name(session_name),
        "-p",
        "OOMPolicy=continue",
    ]
    for prop in _memory_props("", limits):
        argv += ["-p", prop]
    argv.append("--")
    return argv


# --- the gitnexus shim ------------------------------------------------------


def _run_dir() -> str:
    env = (os.environ.get("MINDFLOCK_RUN_DIR") or "").strip()
    if env:
        return env
    # The config dir's rule ($MINDFLOCK_HOME, else $HOME/.mindflock — not
    # expanduser, which reads %USERPROFILE% on Windows): the same run dir
    # the MCP run files use (mcp_attach.run_dir).
    try:
        from backend.config.config import GetConfigDir

        return os.path.join(GetConfigDir(), "run")
    except OSError:
        return os.path.join(os.path.expanduser("~"), ".mindflock", "run")


def shim_dir() -> str:
    return os.path.join(_run_dir(), "bin")


def _package_root() -> str:
    """The directory holding the ``backend`` package this code runs from."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def gitnexus_shim_text(
    python: str,
    heap_mb: int,
    at_once: int = 1,
    wait_seconds: int = GITNEXUS_WAIT_SECONDS,
    package_root: Optional[str] = None,
) -> str:
    """The POSIX sh ``gitnexus`` shim (see the module docstring).

    The real binary is looked up at run time on ``PATH`` with the shim's own
    directory left out, so installing or moving GitNexus later needs nothing
    regenerated. If MindFlock's interpreter is gone (an uninstall), analyze
    still runs — capped, just not queued. ``-P`` keeps the repo being
    committed (the hook's cwd) off ``sys.path``; ``PYTHONPATH`` names the
    package this server runs from, installed or a checkout alike."""
    # Native Windows (Git Bash runs the shim): the shim's own dir as bash's
    # $PATH spells it (/c/…, what the loop below compares), the interpreter
    # and PYTHONPATH as C:/… (native programs take that; bash eats C:\…).
    # What the slot runs: the real binary (Linux/macOS); on Windows the
    # arguments _WIN_SLOT_TARGET puts in front of the caller's.
    win_target = _WIN_SLOT_TARGET if compat.IS_WINDOWS else ""
    target = '"$@"' if compat.IS_WINDOWS else '"$real" "$@"'
    sd = shlex.quote(bash_path(shim_dir()))
    py = shlex.quote(shell_path(python))
    root = shlex.quote(shell_path(package_root or _package_root()))
    return (
        "#!/bin/sh\n"
        f"{SHIM_MARKER} (backend/providers/agent_limits.py) — regenerated at\n"
        "# every agent launch. One `gitnexus analyze` at a time machine-wide, with a\n"
        "# capped heap; everything else is the real gitnexus, untouched.\n"
        f"shim_dir={sd}\n"
        "real=\n"
        "IFS_save=$IFS; IFS=:\n"
        "for d in $PATH; do\n"
        '  [ -n "$d" ] || continue\n'
        '  [ "$d" = "$shim_dir" ] && continue\n'
        '  if [ -x "$d/gitnexus" ] && ! grep -q "^'
        + SHIM_MARKER
        + '" "$d/gitnexus" 2>/dev/null; then\n'
        "    real=$d/gitnexus; break\n"
        "  fi\n"
        "done\n"
        "IFS=$IFS_save\n"
        'if [ -z "$real" ]; then echo "gitnexus: not found on PATH" >&2; exit 127; fi\n'
        'if [ "$1" != analyze ] || [ "${MINDFLOCK_GITNEXUS_GATE:-1}" = 0 ]; then\n'
        '  exec "$real" "$@"\n'
        "fi\n"
        f"{{ echo {GITNEXUS_OOM_SCORE_ADJ} >/proc/self/oom_score_adj; }} 2>/dev/null || :\n"
        'case " ${NODE_OPTIONS-} " in\n'
        "  *max-old-space-size*) ;;\n"
        f'  *) NODE_OPTIONS="${{NODE_OPTIONS:+$NODE_OPTIONS }}--max-old-space-size={int(heap_mb)}"; export NODE_OPTIONS ;;\n'
        "esac\n"
        f"if [ -x {py} ]; then\n"
        + win_target
        + f"  PYTHONPATH={root} exec {py} -P -m backend.slot --pool gitnexus --max {max(1, int(at_once))} "
        f"--timeout {int(wait_seconds)} -- {target}\n"
        "fi\n"
        'exec "$real" "$@"\n'
    )


#: Native Windows: what the slot (a native python.exe) starts for the
#: queued analyze. npm's ``gitnexus`` next to ``gitnexus.cmd`` is an sh
#: script, which only bash can run — CreateProcess refuses it ("not a valid
#: Win32 application") — so the slot gets npm's ``gitnexus.cmd`` (native
#: path), else ``sh.exe <script>``, as ``set --`` arguments ahead of the
#: caller's. (bash's own passthrough above still execs the sh script.)
_WIN_SLOT_TARGET = (
    '  if [ -f "$real.cmd" ]; then\n'
    '    set -- "$(cygpath -w "$real.cmd" 2>/dev/null || echo "$real.cmd")" "$@"\n'
    "  else\n"
    '    set -- "$(cygpath -w "$(command -v sh)")" "$(cygpath -w "$real")" "$@"\n'
    "  fi\n"
)


def _real_gitnexus_on_path(path: Optional[str] = None) -> Optional[str]:
    sd = os.path.realpath(shim_dir())
    for d in (path if path is not None else os.environ.get("PATH", "")).split(
        os.pathsep
    ):
        if not d or os.path.realpath(d) == sd:
            continue
        cand = os.path.join(d, "gitnexus")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def gitnexus_shim_dir(cfg: Optional[Dict[str, object]] = None) -> Optional[str]:
    """Write (or refresh) the ``gitnexus`` shim and return its directory, for
    the front of an agent's ``PATH`` — or None when the gate is off, GitNexus
    is not installed, or the file can't be written."""
    if _env_off("MINDFLOCK_GITNEXUS_GATE"):
        return None
    cfg = config() if cfg is None else cfg
    heap = int(cfg.get("gitnexus_heap_mb") or 0)
    if not cfg.get("enabled", True) or heap <= 0:
        return None
    if not _real_gitnexus_on_path():
        return None
    text = gitnexus_shim_text(
        sys.executable, heap, int(cfg.get("gitnexus_at_once") or 1)
    )
    directory = shim_dir()
    path = os.path.join(directory, "gitnexus")
    try:
        try:
            with open(path, encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = None
        if current != text:
            os.makedirs(directory, exist_ok=True)
            tmp = "%s.%d.tmp" % (path, os.getpid())
            # newline="": a CRLF shim would not run under Git Bash.
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            os.chmod(tmp, 0o755)
            os.replace(tmp, path)
    except OSError:
        return None
    return directory


# --- native Windows ------------------------------------------------------------


def bash_path(path) -> str:
    """``path`` as Git Bash's ``$PATH`` spells a directory (``/c/Users/x``),
    on native Windows; unchanged elsewhere. (Launch shell text otherwise uses
    ``C:/…`` — :func:`backend.providers.launch_script.shell_path` — which
    native programs take too, but a ``$PATH`` entry with a drive colon would
    split at it.)"""
    p = str(path)
    if not compat.IS_WINDOWS:
        return p
    p = p.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(/.*)?$", p)
    if not m:
        return p
    return "/" + m.group(1).lower() + (m.group(2) or "/")


def jobscope_path() -> str:
    """The Windows scope wrapper (:mod:`backend.session.winmux.jobscope`),
    run by path: stdlib only, so it needs nothing importable."""
    return os.path.join(_package_root(), "backend", "session", "winmux", "jobscope.py")


def windows_scope_command(
    limit: int, cmd: str, python: str = "", bash: str = ""
) -> str:
    """``cmd`` (a shell string) run inside a Job Object of ``limit`` bytes:
    ``<python> -I <jobscope.py> --max <limit> -- <bash> -c <cmd>`` — the
    ``systemd-run --scope … sh -c <cmd>`` of Linux. ``-I``: neither the
    script's directory nor the user's site is on ``sys.path``."""
    if not python:
        from .activity_markers import hook_python

        python = hook_python()
    bash = bash or compat.posix_shell(bash=True)
    return "%s -I %s --max %d -- %s -c %s" % (
        shlex.quote(shell_path(python)),
        shlex.quote(shell_path(jobscope_path())),
        int(limit),
        shlex.quote(shell_path(bash)),
        shlex.quote(cmd),
    )


def _wrap_launch_windows(cmd: str) -> str:
    try:
        cfg = config()
        if not cfg.get("enabled", True):
            return cmd
        out = cmd
        limit = resolved_limits(cfg).get("max", 0)
        if limit > 0 and not _env_off("MINDFLOCK_AGENT_SCOPE"):
            out = windows_scope_command(limit, out)
        shims = gitnexus_shim_dir(cfg)
        if shims:
            out = 'PATH=%s:"$PATH"; export PATH; %s' % (
                shlex.quote(bash_path(shims)),
                out,
            )
        return out
    except Exception:  # noqa: BLE001 — a guard never blocks a launch
        return cmd


# --- composing a launch -------------------------------------------------------


def wrap_launch(cmd: str, session_name: str) -> str:
    """``cmd`` (a shell string a ``sh -c`` will run) with the agent guards
    around it: the ``gitnexus`` shim's directory at the front of ``PATH`` and
    the whole launch in its own memory-limited systemd scope. ``cmd`` itself
    when every guard is off or unavailable, so the launch is byte-identical to
    what it was before this module on such a machine.

    The scope runs ``sh -c <cmd>``: ``systemd-run --scope`` execs it in place,
    so the pane's terminal, environment, working directory and exit status
    are the agent's own.

    Native Windows (Git Bash runs the launch): the scope is a Job Object
    with the per-agent ``max`` (:func:`windows_scope_command`, which ends an
    agent over it with exit 137 as Linux's OOM killer would), and the shim's
    directory goes on ``PATH`` as bash spells it (``/c/…``)."""
    if compat.IS_WINDOWS:
        return _wrap_launch_windows(cmd)
    try:
        cfg = config()
        if not cfg.get("enabled", True):
            return cmd
        out = cmd
        if scope_available():
            exe = _systemd_run() or "systemd-run"
            argv = scope_command(session_name, resolved_limits(cfg), exe)
            out = " ".join(shlex.quote(a) for a in argv) + " sh -c " + shlex.quote(cmd)
        shims = gitnexus_shim_dir(cfg)
        if shims:
            out = 'PATH=%s:"$PATH"; export PATH; %s' % (shlex.quote(shims), out)
        return out
    except Exception:  # noqa: BLE001 — a guard never blocks a launch
        return cmd
