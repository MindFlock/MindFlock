"""Per-worktree setup scripts, untracked-file propagation, and check runs.

Roadmap O2 + O3. A fresh worktree is missing everything git doesn't track —
``node_modules``, ``.venv``, ``.env`` — so the agent "fails immediately"
(the single most-requested feature across every competing session manager's
tracker). This module wires the existing :mod:`backend.workspace_setup`
machinery (already used by provisioned workspaces) into *plain* worktree
sessions, configured by a file committed to the target repo:

``<repo>/.mindflock.toml``::

    [workspace]
    setup_commands = ["npm install"]     # run in each new worktree, in order
    copy_untracked = [".env", ".env.local"]  # repo files copied into it first
    check_command  = "npm test"          # O3 verification gate (see below)

Explicit opt-in only: a repo without the file (or without the relevant keys)
gets no setup run and no gate — plain sessions behave exactly as before.
(Provisioned workspaces keep their config.toml auto-detection.)

Both runners execute in a background thread, appending to a log file in the
worktree and maintaining a small status JSON next to it (state
``running`` → ``ok`` | ``failed``). All four artifacts use the
``.mindflock_*`` marker naming and are added to
:data:`backend.workspace_setup.WORKSPACE_ARTIFACTS` so they never appear
in diffs or commits. Events (``session.setup_started`` /
``session.setup_finished`` / ``session.check_started`` /
``session.check_finished``) go over the bus for the UI, shell hooks, and
addons.

The **check run** (O3) is the verification gate: it records the worktree's
HEAD sha when it finishes, so a result only vouches for the commit it ran
against — the push endpoint soft-gates on ``state == ok and sha == HEAD``
and the UI chip shows stale results as such.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional

from backend.web.core import events as _events
from backend.web.core import ports as _ports
from backend.workspace_setup import WorkspaceConfigError, parse_setup_commands

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_NAME = ".mindflock.toml"

SETUP_LOG = ".mindflock_setup.log"
SETUP_STATUS = ".mindflock_setup.json"
CHECK_LOG = ".mindflock_check.log"
CHECK_STATUS = ".mindflock_check.json"

# One live runner per (worktree, kind) so a rerun can't race a running one.
_running_lock = threading.Lock()
_running: set = set()  # {(wt_path, "setup"|"check")}


class WorkspaceConfig:
    """Parsed ``[workspace]`` table of a repo's ``.mindflock.toml``."""

    def __init__(
        self,
        setup_commands: Optional[List[str]] = None,
        copy_untracked: Optional[List[str]] = None,
        check_command: str = "",
        verify_on_push: bool = False,
    ) -> None:
        self.setup_commands = setup_commands  # None = not configured
        self.copy_untracked = copy_untracked or []
        self.check_command = check_command
        # Whether a push in this repo should automatically have a Verify test
        # plan written for it. Default False, and deliberately so: generating a
        # plan is a real model call per push, and a Verify list that fills up on
        # its own destroys the one number the feature exists to show — how many
        # shipped things nobody has checked. A badge that over-counts stops being
        # read. So the button on a session is the normal way to get a plan, and
        # this key is how a repo whose work always warrants a manual test says
        # "do it for me" — the same explicit-opt-in bargain ``check_command``
        # already makes two fields up.
        self.verify_on_push = verify_on_push

    @property
    def has_setup(self) -> bool:
        return bool(self.setup_commands) or bool(self.copy_untracked)


def load_config(repo_path: str) -> WorkspaceConfig:
    """Read ``<repo>/.mindflock.toml``. Missing/invalid file = empty config.

    A malformed file is reported (once per read, via the returned empty
    config's log line) rather than raised — session creation must never
    fail because of a typo in an optional config file.
    """
    path = os.path.join(repo_path or "", CONFIG_NAME)
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return WorkspaceConfig()
    except Exception:  # noqa: BLE001 — malformed toml
        return WorkspaceConfig()
    try:
        commands = parse_setup_commands(raw)
    except WorkspaceConfigError:
        commands = None
    ws = raw.get("workspace", {}) or {}
    copy_untracked = ws.get("copy_untracked") or []
    if not (
        isinstance(copy_untracked, list)
        and all(isinstance(p, str) for p in copy_untracked)
    ):
        copy_untracked = []
    check = ws.get("check_command", "")
    if not isinstance(check, str):
        check = ""
    # Strict ``is True``: only the boolean turns this on. A repo that wrote
    # ``verify_on_push = "no"`` or ``= 0`` meant no, and Python's truthiness
    # would read the string as yes — spending a model call per push on the
    # strength of a typo. Anything that is not literally ``true`` is off, which
    # is also the safe direction for a key whose default is off.
    return WorkspaceConfig(
        setup_commands=commands,
        copy_untracked=[p.strip() for p in copy_untracked if p.strip()],
        check_command=check.strip(),
        verify_on_push=ws.get("verify_on_push") is True,
    )


# --- untracked-file propagation --------------------------------------------
def copy_untracked(repo_path: str, wt_path: str, patterns: List[str]) -> List[str]:
    """Copy declared untracked files/dirs from the repo into the worktree.

    Paths are repo-root-relative; escapes (``..``, absolute) are rejected.
    Never overwrites something already in the worktree and never raises —
    a missing source is simply skipped. Returns the paths actually copied.
    """
    copied: List[str] = []
    repo = Path(repo_path or "").resolve()
    wt = Path(wt_path or "").resolve()
    for rel in patterns:
        try:
            src = (repo / rel).resolve()
            if not str(src).startswith(str(repo) + os.sep):
                continue  # path escaped the repo root
            dst = wt / rel
            if not src.exists() or dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            copied.append(rel)
        except Exception:  # noqa: BLE001 — best-effort per entry
            continue
    return copied


# --- status files ------------------------------------------------------------
def _read_status(wt_path: str, name: str) -> Optional[dict]:
    try:
        with open(os.path.join(wt_path, name), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _write_status(wt_path: str, name: str, status: dict) -> None:
    path = os.path.join(wt_path, name)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — status is advisory
        pass


def setup_status(wt_path: str) -> Optional[dict]:
    """``{state, rc, started, finished, commands, copied}`` or None."""
    return _read_status(wt_path, SETUP_STATUS)


def check_status(wt_path: str) -> Optional[dict]:
    """``{state, rc, sha, command, started, finished}`` or None."""
    return _read_status(wt_path, CHECK_STATUS)


def clear_check(wt_path: str) -> bool:
    """Forget the recorded check result. Returns whether a file was removed.

    Only ever called for a result that is already STALE (its sha is not HEAD) —
    the push gate reads this file, so deleting a failure that still describes the
    current commit would silently un-gate the very push it exists to hold.
    """
    try:
        os.unlink(os.path.join(wt_path, CHECK_STATUS))
        return True
    except Exception:  # noqa: BLE001 — absent (or unremovable) is a no-op
        return False


def log_tail(wt_path: str, name: str, lines: int = 200) -> str:
    """Last ``lines`` lines of a runner log ("" when absent)."""
    try:
        with open(os.path.join(wt_path, name), encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-max(1, min(lines, 2000)) :])
    except Exception:  # noqa: BLE001
        return ""


def _head_sha(wt_path: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", wt_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


# --- runners -----------------------------------------------------------------
def _run_logged(
    commands: List[str], wt_path: str, log_name: str, title: str = ""
) -> int:
    """Run shell commands in order, appending output to the log. Returns rc.

    Commands see the session's O4 port block (``PORT`` etc.) so a setup step
    (seed a DB, prebuild) and the agent's dev server agree on ports.
    """
    env = dict(os.environ)
    if title:
        try:
            env.update(_ports.env_for(title))
        except Exception:  # noqa: BLE001 — ports are a nicety, not a gate
            pass
    log_path = os.path.join(wt_path, log_name)
    with open(log_path, "a", encoding="utf-8") as logf:
        for command in commands:
            logf.write("$ %s\n" % command)
            logf.flush()
            try:
                cp = subprocess.run(
                    command,
                    shell=True,
                    cwd=wt_path,
                    env=env,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    timeout=900,  # generous: installs/builds, but never forever
                )
            except subprocess.TimeoutExpired:
                logf.write("[timed out after 900s: %s]\n" % command)
                return 124
            if cp.returncode != 0:
                logf.write("[exit %d]\n" % cp.returncode)
                return cp.returncode
    return 0


def _note_crash(wt_path: str, log_name: str, err: BaseException) -> None:
    """Append a runner-body crash to the run log so a ``failed`` status has a
    trace. Best-effort: never raises (a failing log write must not mask the
    original crash or, worse, re-wedge the status)."""
    try:
        with open(os.path.join(wt_path, log_name), "a", encoding="utf-8") as logf:
            logf.write("[runner crashed: %r]\n" % err)
    except OSError:
        pass


def _runner(kind: str, title: str, wt_path: str, run) -> None:
    key = (wt_path, kind)
    with _running_lock:
        if key in _running:
            return
        _running.add(key)
    try:
        run()
    finally:
        with _running_lock:
            _running.discard(key)


def is_running(wt_path: str, kind: str) -> bool:
    with _running_lock:
        return (wt_path, kind) in _running


def start_setup(
    title: str, repo_path: str, wt_path: str, cfg: Optional[WorkspaceConfig] = None
) -> bool:
    """Kick off the setup pass for a fresh worktree (background thread).

    Copies ``copy_untracked`` entries synchronously (fast, must precede any
    setup command that reads them), then runs ``setup_commands`` in a
    thread. Returns False when nothing is configured or a run is already
    live for this worktree.
    """
    cfg = cfg or load_config(repo_path)
    if not cfg.has_setup or not wt_path or not os.path.isdir(wt_path):
        return False
    if is_running(wt_path, "setup"):
        return False
    copied = copy_untracked(repo_path, wt_path, cfg.copy_untracked)
    commands = list(cfg.setup_commands or [])
    status = {
        "state": "running",
        "rc": None,
        "started": time.time(),
        "finished": None,
        "commands": commands,
        "copied": copied,
    }
    _write_status(wt_path, SETUP_STATUS, status)
    _events.BUS.emit(
        "session.setup_started",
        session=title,
        data={"commands": commands, "copied": copied},
    )

    def run() -> None:
        # `status` starts as "running"; always drive it to a terminal state so
        # the session card can't wedge at "running" if the body raises (a stray
        # OSError from a command spawn used to strand it there forever).
        rc = 1
        try:
            rc = _run_logged(commands, wt_path, SETUP_LOG, title) if commands else 0
        except Exception as err:  # noqa: BLE001
            _note_crash(wt_path, SETUP_LOG, err)
        status.update(state="ok" if rc == 0 else "failed", rc=rc, finished=time.time())
        _write_status(wt_path, SETUP_STATUS, status)
        _events.BUS.emit(
            "session.setup_finished",
            session=title,
            new=status["state"],
            data={"rc": rc},
        )

    threading.Thread(
        target=_runner,
        args=("setup", title, wt_path, run),
        name="mindflock-setup-%s" % title,
        daemon=True,
    ).start()
    return True


def start_check(title: str, wt_path: str, command: str) -> bool:
    """Kick off a check (verification) run in the worktree. See module doc."""
    if not command or not wt_path or not os.path.isdir(wt_path):
        return False
    if is_running(wt_path, "check"):
        return False
    status = {
        "state": "running",
        "rc": None,
        "sha": "",
        "command": command,
        "started": time.time(),
        "finished": None,
    }
    _write_status(wt_path, CHECK_STATUS, status)
    _events.BUS.emit("session.check_started", session=title, data={"command": command})

    def run() -> None:
        # Always reach a terminal state even if the body raises (see start_setup).
        rc = 1
        try:
            rc = _run_logged([command], wt_path, CHECK_LOG, title)
        except Exception as err:  # noqa: BLE001
            _note_crash(wt_path, CHECK_LOG, err)
        try:
            sha = _head_sha(wt_path)
        except Exception:  # noqa: BLE001
            sha = ""
        status.update(
            state="ok" if rc == 0 else "failed",
            rc=rc,
            finished=time.time(),
            sha=sha,
        )
        _write_status(wt_path, CHECK_STATUS, status)
        _events.BUS.emit(
            "session.check_finished",
            session=title,
            new=status["state"],
            data={"rc": rc, "sha": status["sha"]},
        )

    threading.Thread(
        target=_runner,
        args=("check", title, wt_path, run),
        name="mindflock-check-%s" % title,
        daemon=True,
    ).start()
    return True


def check_summary(wt_path: str) -> Optional[dict]:
    """Check state for the session card: status + staleness vs current HEAD."""
    st = check_status(wt_path)
    if st is None:
        return None
    state = st.get("state")
    if state in ("ok", "failed"):
        head = _head_sha(wt_path)
        st = dict(st)
        st["stale"] = bool(head) and st.get("sha") not in ("", head)
    return st


def setup_summary(wt_path: str) -> Optional[dict]:
    """Setup state for the session card (status file as-is)."""
    return setup_status(wt_path)
