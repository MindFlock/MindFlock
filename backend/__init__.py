"""MindFlock — a private flock of AI coding agents, started by your ticket queue.

The version is single-sourced from ``pyproject.toml`` (the uv_build backend
does not support dynamic metadata, so the static ``[project] version`` there
is canonical and this module reads it back from the installed distribution).
"""

import os as _os
import sys as _sys
from importlib.metadata import PackageNotFoundError, version


# Windows: uv's ``mindflock.exe`` shim runs ``python mindflock.exe …`` — the
# exe is a zipapp, and it is ``sys.path[0]``. importlib.metadata (``version()``
# just below, and any library asking later) scans sys.path and keeps a
# ZipFile open on every zip it meets for the life of the process, so each
# mindflock process pinned its own shim: Windows then refuses to rename or
# replace it, and an engine update failed on "mindflock.exe is being used by
# another process" while a server ran (docs/windows.md, "Updating the engine
# on Windows"). ``__main__`` has already run from it; nothing else lives there.
def _drop_shim_from_path(path=_sys.path, windows=_os.name == "nt") -> None:
    if (
        windows
        and path
        and path[0].lower().endswith(".exe")
        and _os.path.isfile(path[0])
    ):
        del path[0]


_drop_shim_from_path()


def _checkout_version(pkg_dir: str = "") -> str:
    """A development checkout's OWN version: ``[project] version`` of the
    ``pyproject.toml`` beside this package, "" when there is none (a release
    install keeps ``backend/`` in site-packages, with no pyproject beside it).

    An editable install's dist-info is written once, at install time, so
    after a ``git pull`` that bumps the version it named the old one until
    someone reinstalled by hand. The checkout's code is what this process
    imported, so the checkout's pyproject is what it runs."""
    pkg_dir = pkg_dir or _os.path.dirname(_os.path.abspath(__file__))
    path = _os.path.join(_os.path.dirname(pkg_dir), "pyproject.toml")
    if not _os.path.isfile(path):
        return ""
    try:
        import tomllib

        with open(path, "rb") as fh:
            project = tomllib.load(fh).get("project") or {}
    except Exception:  # noqa: BLE001 — a version line never breaks an import
        return ""
    if project.get("name") != "mindflock":
        return ""
    return str(project.get("version") or "").strip()


try:
    __version__ = _checkout_version() or version("mindflock")
except PackageNotFoundError:  # source tree without an installed dist
    __version__ = "0+unknown"
