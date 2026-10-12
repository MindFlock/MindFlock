"""Small file-store helpers: atomic writes and forgiving JSON reads.

Stdlib only and imports nothing from ``backend``, so every layer (config,
session, providers, web, ticket_ingestion) can use it without an import cycle.
``backend/mcp`` is a standalone stdlib-only server and keeps its own copies.

Writes go to a temp file in the target's directory, are fsynced, then
``os.replace``d over the target: a reader (or a crash) sees the old file or
the new one, never a truncated one. The temp file is removed on any error.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional, Tuple, Union

PathLike = Union[str, "os.PathLike[str]"]


def atomic_write_bytes(
    path: PathLike,
    data: bytes,
    *,
    mode: Optional[int] = None,
    prefix: Optional[str] = None,
    suffix: str = ".tmp",
) -> None:
    """Atomically replace ``path`` with ``data``; creates the parent dir.

    The new file is mode 0600 (``mkstemp``'s) unless ``mode`` is given, which
    is applied exactly (not umask-masked) before any data is written.
    ``prefix`` defaults to ``.<basename>.``. Raises on failure.
    """
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    if prefix is None:
        prefix = "." + os.path.basename(path) + "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    try:
        if mode is not None:
            os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: PathLike,
    text: str,
    *,
    mode: Optional[int] = None,
    prefix: Optional[str] = None,
    suffix: str = ".tmp",
    encoding: str = "utf-8",
) -> None:
    """:func:`atomic_write_bytes` for text (no newline translation)."""
    atomic_write_bytes(
        path, text.encode(encoding), mode=mode, prefix=prefix, suffix=suffix
    )


def atomic_write_json(
    path: PathLike,
    obj: Any,
    *,
    indent: Optional[int] = 2,
    mode: Optional[int] = None,
    newline: bool = True,
    prefix: Optional[str] = None,
    suffix: str = ".tmp",
    **dumps_kwargs: Any,
) -> None:
    """Atomically write ``obj`` as JSON (UTF-8, ``ensure_ascii=False`` unless
    overridden, plus a trailing newline unless ``newline=False``). Extra
    keyword arguments go to :func:`json.dumps` (e.g. ``sort_keys=True``)."""
    dumps_kwargs.setdefault("ensure_ascii", False)
    text = json.dumps(obj, indent=indent, **dumps_kwargs)
    if newline:
        text += "\n"
    atomic_write_text(path, text, mode=mode, prefix=prefix, suffix=suffix)


def read_json(
    path: PathLike,
    default: Any = None,
    *,
    expect: Optional[Union[type, Tuple[type, ...]]] = None,
) -> Any:
    """The parsed JSON at ``path``, or ``default`` when the file is missing,
    unreadable (``OSError``) or not valid JSON (``ValueError``). With
    ``expect``, a document that is not an instance of it also reads as
    ``default``."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    if expect is not None and not isinstance(data, expect):
        return default
    return data
