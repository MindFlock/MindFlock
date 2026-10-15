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

**Installed drivers survive updates.** An engine update rebuilds the venv from
``mindflock[web]`` alone, which used to drop every driver installed here. A
successful install is recorded in ``~/.mindflock/dbclient/drivers.json``
(:func:`backend.web.core.self_update.record_driver`); the updater carries the
recorded packages through the rebuild, and for any rebuild it did not run
(``install.sh``, a hand-run ``uv tool install --force``) :func:`start_self_heal`
reinstalls a recorded-but-missing driver in the background, once per engine
per boot — the report says "Reinstalling …" meanwhile instead of offering the
button.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backend.web.core import self_update

from .adapters import driver_report, get_adapter

#: Wheels are small but a source build (no wheel for this platform) is not, and
#: a stalled index must not hold the request forever.
INSTALL_TIMEOUT_S = 300

#: How much of the installer's output travels back to the UI (the tail is the
#: part that explains a failure).
OUTPUT_TAIL_CHARS = 4000

#: Serializes installs: two clicks must not run two resolvers over one venv.
_LOCK = threading.Lock()

#: Engines the self-heal has already tried this boot (it tries each once).
_HEAL_TRIED: set = set()
#: ``{engine: {"state": "running"|"failed", "note": str}}`` for the report.
_HEAL_STATE: Dict[str, dict] = {}
#: Guards the two above (the report reads them from request threads).
_HEAL_GUARD = threading.Lock()

#: The installer saying the package does not exist (pip / uv wording): the
#: only failure that drops a driver from the record.
_NO_SUCH_PACKAGE = re.compile(
    r"no matching distribution|not found in the package registry", re.IGNORECASE
)


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
    start_self_heal()
    reason = blocked_reason()
    label = installer_label()
    drivers: List[dict] = driver_report()
    with _HEAL_GUARD:
        heal = {k: dict(v) for k, v in _HEAL_STATE.items()}
    for row in drivers:
        missing = not row["available"] and bool(row["driver"])
        healing = heal.get(row["engine"]) if missing else None
        reinstalling = bool(healing) and healing["state"] == "running"
        # While the self-heal runs, the note says so instead of a button.
        row["can_install"] = bool(label) and missing and not reinstalling
        # Carried per row (not just once at the top) so the UI's driver cache —
        # a plain list of rows — has everything the note needs to explain why
        # there is no button.
        row["install_blocked"] = reason if missing and not label else ""
        row["reinstall"] = healing["state"] if healing else ""
        row["reinstall_note"] = healing["note"] if healing else ""
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
        available, method, code, output = _attempt(adapter, package)
    finally:
        _LOCK.release()
    # This click's outcome supersedes whatever the self-heal last reported.
    _set_heal(adapter.engine)
    if available:
        self_update.record_driver(adapter.engine, package)
    return _report(base, adapter, available, method, code, output)


def _attempt(adapter, package: str) -> Tuple[bool, str, int, str]:
    """Run the install plans for ``package`` (the caller holds ``_LOCK``) and
    re-ask the adapter. Returns ``(available, method, code, output)``."""
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

    method = attempts[-1][0] if attempts else ""
    code = attempts[-1][2] if attempts else 1
    output = (
        attempts[-1][1]
        if code == 0
        else "\n\n".join("$ %s\n%s" % (label, out) for label, out, _ in attempts)
    )
    return available, method, code, output


def _report(
    base: dict, adapter, available: bool, method: str, code: int, output: str
) -> dict:
    package = adapter.driver
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


# --------------------------------------------------------------------------- #
# Self-heal: drivers an engine update dropped
# --------------------------------------------------------------------------- #


def _display_name(package: str) -> str:
    """``psycopg[binary]`` → ``psycopg``: the name a person knows it by."""
    return re.split(r"[\[<>=!~;\s]", package, maxsplit=1)[0] or package


def _heal_candidates() -> List[str]:
    """Recorded engines whose driver is missing and not yet tried this boot."""
    out: List[str] = []
    for engine in sorted(self_update.read_driver_record()):
        if engine in _HEAL_TRIED:
            continue
        try:
            adapter = get_adapter(engine)
        except ValueError:  # an engine this build no longer has
            continue
        if adapter.driver and not adapter.available():
            out.append(engine)
    return out


def _set_heal(engine: str, state: str = "", note: str = "") -> None:
    with _HEAL_GUARD:
        if state:
            _HEAL_STATE[engine] = {"state": state, "note": note}
        else:
            _HEAL_STATE.pop(engine, None)


def start_self_heal() -> Optional[threading.Thread]:
    """Reinstall every recorded driver the venv lost (an engine update or a
    reinstall rebuilt it) — in a daemon thread, each engine at most once per
    boot. Returns the thread, or ``None`` when there is nothing to do. Called
    at addon startup and on every ``GET /drivers``; cheap when idle."""
    try:
        with _HEAL_GUARD:
            engines = _heal_candidates()
            _HEAL_TRIED.update(engines)
    except Exception:  # noqa: BLE001 — the report must never fail over this
        return None
    if not engines:
        return None
    for engine in engines:
        _set_heal(
            engine,
            "running",
            "Reinstalling %s after the update…"
            % _display_name(get_adapter(engine).driver),
        )
    thread = threading.Thread(
        target=_self_heal, args=(engines,), name="dbclient-driver-heal", daemon=True
    )
    thread.start()
    return thread


def _self_heal(engines: List[str]) -> None:
    for engine in engines:
        try:
            _heal_one(engine)
        except Exception as err:  # noqa: BLE001 — one engine never stops the rest
            _set_heal(engine, "failed", "last reinstall failed: %s" % err)


def _heal_one(engine: str) -> None:
    adapter = get_adapter(engine)
    package = adapter.driver
    reason = blocked_reason()
    if reason:
        _set_heal(engine, "failed", "last reinstall failed: %s" % reason)
        return
    # Blocking, unlike a click: a user install already running is simply
    # waited for (and probably makes this one moot).
    with _LOCK:
        if adapter.available():
            available, code, output = True, 0, ""
        else:
            available, _method, code, output = _attempt(adapter, package)
    if available:
        self_update.record_driver(engine, package)
        _set_heal(engine)
        return
    _set_heal(engine, "failed", "last reinstall failed: %s" % _tail(output))
    # The package itself is gone from the index: stop carrying it, or every
    # boot and every update would trip over it again.
    if code != 0 and _NO_SUCH_PACKAGE.search(output or ""):
        self_update.forget_driver(engine)
