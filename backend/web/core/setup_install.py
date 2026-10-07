"""One-click "Install everything missing".

Runs the doctor's one-shot install script (:func:`backend.doctor.install_plan`)
in a throwaway tmux session the browser attaches to through the same
PTY<->websocket bridge every other MindFlock terminal uses — a real terminal,
because ``sudo`` has to ask for a password and the user has to see apt work.

The script is always rebuilt HERE from a fresh doctor run; nothing the client
sends becomes a command. Shaped like :mod:`backend.web.core.provider_login`,
with one difference that matters: closing the window must not kill an install
halfway through an ``apt-get``, so the session records its exit status in a
marker file and is only torn down (or replaced by a fresh run) once that file
exists.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Optional, Tuple

SESSION = "mindflock_setup_install"
_DN = subprocess.DEVNULL


def _run_dir() -> str:
    from backend.providers import mcp_attach

    return mcp_attach.run_dir()


def _script_path() -> str:
    return os.path.join(_run_dir(), "setup-install.sh")


def _status_path() -> str:
    return os.path.join(_run_dir(), "setup-install.status")


def _tmux(*args: str) -> int:
    try:
        return subprocess.run(
            ["tmux", *args], stdout=_DN, stderr=_DN, timeout=10
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def _session_exists() -> bool:
    return _tmux("has-session", "-t=" + SESSION) == 0


def exit_code() -> Optional[int]:
    """The finished script's exit status, or ``None`` while it runs (or before
    it ever ran)."""
    try:
        with open(_status_path(), encoding="utf-8") as fh:
            return int(fh.read().strip() or "1")
    except (OSError, ValueError):
        return None


def state() -> dict:
    """``{"running": bool, "exit_code": int | None}`` for the UI to follow."""
    code = exit_code()
    return {"running": _session_exists() and code is None, "exit_code": code}


def _write_script(script: str) -> str:
    os.makedirs(_run_dir(), mode=0o700, exist_ok=True)
    path = _script_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(script)
    return path


def ensure_session() -> Tuple[str, Optional[str]]:
    """Ensure a tmux session running the install script exists.

    Returns ``(session_name, error_or_None)``. A run still in progress is
    reattached, never restarted; a finished one is replaced by a fresh run
    built from a fresh doctor probe (so "Install" after a partial failure
    retries only what is still missing). Nothing to install is an error the
    UI shows, not an empty terminal.
    """
    if _session_exists():
        if exit_code() is None:
            return SESSION, None
        _tmux("kill-session", "-t=" + SESSION)
    # Forget the previous run's result FIRST — the probe below takes seconds,
    # and a UI polling in that window must not read the old exit status as
    # this run's.
    try:
        os.unlink(_status_path())
    except FileNotFoundError:
        pass
    except OSError as err:
        return SESSION, str(err)
    from backend import doctor

    plan = doctor.install_plan(doctor.run_checks())
    if not plan["steps"]:
        return SESSION, "nothing to install — every needed dependency is present"
    try:
        script = _write_script(plan["script"])
    except OSError as err:
        return SESSION, "could not write the install script: %s" % err
    status = shlex.quote(_status_path())
    # Record the exit status BEFORE handing the pane to a shell: that file is
    # what says "safe to close" and what the UI watches to re-run the doctor.
    wrapped = (
        "sh %s; echo $? > %s; echo; "
        "echo '[mindflock] done — you can close this window'; "
        "exec ${SHELL:-/bin/sh}" % (shlex.quote(script), status)
    )
    try:
        created = subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                SESSION,
                "-c",
                os.path.expanduser("~"),
                "sh",
                "-c",
                wrapped,
            ],
            stdout=_DN,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return SESSION, "tmux new-session failed: %s" % err
    if created.returncode != 0:
        if _session_exists():  # lost a race with a second tab — same session
            return SESSION, None
        return (
            SESSION,
            created.stderr.decode("utf-8", "replace").strip() or "tmux failed",
        )
    for opt, val in (
        ("mouse", "on"),
        ("history-limit", "10000"),
        ("window-size", "latest"),
    ):
        _tmux("set-option", "-t", SESSION, opt, val)
    return SESSION, None


def close() -> bool:
    """Tear the session down once the script has finished. Returns whether it
    was closed — a run still in progress keeps going in the background (the
    next "Install" reattaches to it)."""
    if not _session_exists():
        return True
    if exit_code() is None:
        return False
    _tmux("kill-session", "-t=" + SESSION)
    return True
