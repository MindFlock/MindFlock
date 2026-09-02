"""One-click driver install for the Database Client extension.

The app is itself a Python program, so "install the PostgreSQL driver" never
means "install Python" for the user: the target is always the environment this
process already runs in (``sys.prefix``), and the documented install — ``uv
tool install`` — puts both the interpreter and ``uv`` on the machine. That is
what makes a button possible where the UI used to only print a command.

Three installers are tried, first available wins:

  1. ``uv pip install --python <sys.executable>`` — the uv layout's own manager.
  2. ``<sys.executable> -m pip install`` — any venv that has pip (pipx, a plain
     ``python -m venv``, a dev checkout).
  3. ``ensurepip`` and then (2) — a venv created without pip.

Two guardrails matter more than the plumbing:

* **Nothing HTTP-supplied is ever installed.** A request names an *engine*; the
  package spec comes from that engine's adapter (``Adapter.driver``), so the
  worst a caller can do is install a driver the app already vendors a hint for.
* **A system interpreter is refused, not broken.** On a PEP 668 "externally
  managed" Python outside a venv both installers would need
  ``--break-system-packages``; the report says so and the UI keeps showing the
  manual command instead of offering a button that would damage the OS.

The install lands in a live process, so :func:`install_driver` invalidates the
import caches and re-runs the adapter's own ``available()`` before reporting
success — no restart, and no claim of success the next query would disprove.
One install runs at a time (``_LOCK``); a second click is told so rather than
racing the first.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .adapters import driver_report, get_adapter

#: Wheels are small but a source build (no wheel for this platform) is not, and
#: a stalled index must not hold the request forever.
INSTALL_TIMEOUT_S = 300

#: How much of the installer's output travels back to the UI (the tail is the
#: part that explains a failure).
OUTPUT_TAIL_CHARS = 4000

#: Serializes installs: two clicks must not run two resolvers over one venv.
_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Capability probes
# --------------------------------------------------------------------------- #


def _uv_path() -> Optional[str]:
    """``uv`` on PATH, or in the places its own installer puts it (the server's
    PATH is inherited from wherever it was launched — a desktop launcher's PATH
    often lacks ``~/.local/bin``)."""
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    for cand in (
        home / ".local" / "bin" / "uv",
        home / ".cargo" / "bin" / "uv",
        Path("/opt/homebrew/bin/uv"),
        Path("/usr/local/bin/uv"),
    ):
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        except OSError:  # pragma: no cover — unreadable path
            continue
    return None


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover — broken meta path
        return False


def _has_pip() -> bool:
    return _has_module("pip")


def _has_ensurepip() -> bool:
    """``ensurepip`` is stdlib, so it is there even in a venv built without pip
    — which is exactly the venv that needs it."""
    return _has_module("ensurepip")


def _in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _externally_managed() -> bool:
    """PEP 668: the interpreter's packages belong to the OS package manager."""
    for key in ("stdlib", "platstdlib"):
        path = sysconfig.get_paths().get(key)
        if path and Path(path, "EXTERNALLY-MANAGED").exists():
            return True
    return False


def blocked_reason() -> str:
    """Why an in-app install is refused here, or ``""`` when it is offered."""
    if not _in_venv() and _externally_managed():
        return (
            "the server runs on a system-managed Python (%s), where installing "
            "a package needs your OS package manager" % sys.prefix
        )
    if not _uv_path() and not _has_pip() and not _has_ensurepip():
        return "neither uv nor pip is available to the server's interpreter"
    return ""


def installer_label() -> str:
    """Short name of the installer that would run, or ``""`` if none can."""
    if blocked_reason():
        return ""
    return "uv" if _uv_path() else "pip"


def drivers_payload() -> dict:
    """``GET /drivers``: the adapter report plus whether this server can do the
    install itself, so the UI offers a button only when it would really work."""
    reason = blocked_reason()
    label = installer_label()
    drivers: List[dict] = driver_report()
    for row in drivers:
        missing = not row["available"] and bool(row["driver"])
        row["can_install"] = bool(label) and missing
        # Carried per row (not just once at the top) so the UI's driver cache —
        # a plain list of rows — has everything the note needs to explain why
        # there is no button.
        row["install_blocked"] = reason if missing and not label else ""
    return {
        "drivers": drivers,
        "installer": label,
        "install_blocked": reason,
        "target": sys.prefix,
    }


# --------------------------------------------------------------------------- #
# Running the install
# --------------------------------------------------------------------------- #


def _child_env() -> Dict[str, str]:
    """The installer must target OUR interpreter, not whatever venv the shell
    that launched the server happened to be in — so the vars that would steer
    it elsewhere are dropped, and the noise-makers are turned off."""
    env = dict(os.environ)
    for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    env["NO_COLOR"] = "1"
    env["UV_NO_PROGRESS"] = "1"
    env["UV_PYTHON_DOWNLOADS"] = "never"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    return env


def _plans(package: str) -> List[Tuple[str, List[List[str]]]]:
    """``[(label, [argv, …])]`` — each plan's steps run in order, and the next
    plan is tried only if the previous one failed."""
    plans: List[Tuple[str, List[List[str]]]] = []
    uv = _uv_path()
    if uv:
        plans.append(
            ("uv pip", [[uv, "pip", "install", "--python", sys.executable, package]])
        )
    pip = [sys.executable, "-m", "pip", "install", package]
    if _has_pip():
        plans.append(("pip", [pip]))
    elif _has_ensurepip():
        plans.append(("ensurepip + pip", [[sys.executable, "-m", "ensurepip"], pip]))
    return plans


def _run(argv: List[str]) -> Tuple[int, str]:
    """(exit code, combined output). A timeout and a missing binary come back as
    a non-zero code with the reason in the output, never as an exception."""
    try:
        proc = subprocess.run(  # noqa: S603 — argv is built here, never from HTTP
            argv,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_S,
            env=_child_env(),
            # A neutral cwd: `uv pip` reads [tool.uv] from the directory it runs
            # in, and the server's cwd is a user repo.
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 1, "timed out after %ds: %s" % (INSTALL_TIMEOUT_S, " ".join(argv))
    except OSError as err:
        return 1, "%s: %s" % (" ".join(argv), err)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) <= OUTPUT_TAIL_CHARS:
        return text
    return "…" + text[-OUTPUT_TAIL_CHARS:]


def install_driver(engine: str) -> dict:
    """Install ``engine``'s driver into this server's own environment.

    Returns ``{ok, engine, driver, already?, method?, output?, error?,
    install_hint?, target}``. Raises :class:`ValueError` for an unknown engine
    (the router maps that to a 400); every other failure is a report, because
    "the install did not work and here is what pip said" is information the UI
    must show, not an exception to swallow.
    """
    adapter = get_adapter(engine)  # ValueError for anything not in ADAPTERS
    package = adapter.driver
    base = {"engine": adapter.engine, "driver": package, "target": sys.prefix}
    if not package or adapter.available():
        return {**base, "ok": True, "already": True}

    reason = blocked_reason()
    if reason:
        return {
            **base,
            "ok": False,
            "error": "cannot install from here — %s" % reason,
            "install_hint": adapter.install_hint(),
        }

    if not _LOCK.acquire(blocking=False):
        return {
            **base,
            "ok": False,
            "busy": True,
            "error": "another driver install is already running — wait for it to finish",
        }
    try:
        # Every attempt is kept: when the last plan fails too, "uv said X and
        # then pip said Y" is the diagnosis, and dropping the earlier half of
        # it would be dropping the half that usually explains the failure.
        attempts: List[Tuple[str, str, int]] = []
        for label, steps in _plans(package):
            out_parts: List[str] = []
            code = 0
            for argv in steps:
                code, out = _run(argv)
                out_parts.append(out)
                if code != 0:
                    break
            attempts.append((label, "\n".join(p for p in out_parts if p), code))
            if code == 0:
                break
        # New files in a directory already on sys.path: the finders' caches are
        # the only thing standing between us and importing them right now.
        importlib.invalidate_caches()
        available = adapter.available()
    finally:
        _LOCK.release()

    method = attempts[-1][0] if attempts else ""
    code = attempts[-1][2] if attempts else 1
    output = (
        attempts[-1][1]
        if code == 0
        else "\n\n".join("$ %s\n%s" % (label, out) for label, out, _ in attempts)
    )

    if available:
        return {**base, "ok": True, "method": method, "output": _tail(output)}
    if code == 0:
        return {
            **base,
            "ok": False,
            "method": method,
            "output": _tail(output),
            "error": "%s finished, but %s is still not importable in the server's "
            "environment (%s)" % (method, package, sys.prefix),
            "install_hint": adapter.install_hint(),
        }
    return {
        **base,
        "ok": False,
        "method": method,
        "output": _tail(output),
        "error": "%s failed to install %s" % (method or "the installer", package),
        "install_hint": adapter.install_hint(),
    }
