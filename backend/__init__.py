"""MindFlock — turn your ticket queue into a queue of pull requests.

The version is single-sourced from ``pyproject.toml`` (the uv_build backend
does not support dynamic metadata, so the static ``[project] version`` there
is canonical and this module reads it back from the installed distribution).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mindflock")
except PackageNotFoundError:  # source tree without an installed dist
    __version__ = "0+unknown"
