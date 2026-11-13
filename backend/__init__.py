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

try:
    __version__ = version("mindflock")
except PackageNotFoundError:  # source tree without an installed dist
    __version__ = "0+unknown"
