"""Pasted/dropped file handling for the ``/api/paste-image`` endpoint.

Where pasted screenshots may live (the global ``~/.mindflock/pastes`` plus each
session workspace's ``.mindflock_pastes``), the retention pruning that keeps
only the newest few per directory, and client-filename sanitisation.

Split out of ``backend.web.server`` (which re-imports these names — the
paste route and tests reference them through the server namespace).
"""

from __future__ import annotations

import os
import re
import stat


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


#: Pasted screenshots are transient input for a live conversation, not
#: artifacts: keep only the newest few per directory so phone screenshots
#: never accumulate disk.
_PASTE_KEEP = 10


def _paste_dirs() -> list:
    """Every directory pasted images may live in: the global
    ``~/.mindflock/pastes`` plus each known session workspace's
    ``.mindflock_pastes``."""
    srv = _server()
    dirs = [os.path.join(os.path.expanduser("~"), ".mindflock", "pastes")]
    for inst in list(srv.ENGINE.instances.values()):
        try:
            folder = inst.GetWorktreePath() if inst.Started() else (inst.Path or "")
        except Exception:  # noqa: BLE001
            folder = getattr(inst, "Path", "") or ""
        if folder:
            dirs.append(os.path.join(folder, WORKSPACE_PASTE_DIR))
    return dirs


#: A workspace's paste folder. The session's agent owns everything in its
#: workspace and may have replaced it with a symlink (a shared-folder (peer)
#: session's agent is untrusted), so it is only ever reached through
#: ``O_NOFOLLOW`` directory fds — never by path.
WORKSPACE_PASTE_DIR = ".mindflock_pastes"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _prune_fd(dfd: int, keep: int) -> None:
    """:func:`_prune_pastes` on an open directory fd: only regular
    ``paste-*`` files directly in it, unlinked relative to the fd."""
    try:
        names = os.listdir(dfd)
    except OSError:
        return
    stamped = []
    for n in names:
        if not n.startswith("paste-"):
            continue
        try:
            st = os.stat(n, dir_fd=dfd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            stamped.append((st.st_mtime, n))
    victims = sorted(stamped)[:-keep] if keep > 0 else sorted(stamped)
    for _, n in victims:
        try:
            os.unlink(n, dir_fd=dfd)
        except OSError:
            pass


def _prune_pastes(base: str, keep: int = _PASTE_KEEP) -> None:
    """Delete all but the ``keep`` newest ``paste-*`` files in ``base``.

    Only files this endpoint itself named (``paste-<stamp>-<hex>.<ext>``) are
    ever touched, so a user file that wandered into the directory is safe. A
    ``base`` that is a symlink is left alone (a workspace's agent could point
    it anywhere on the host). Never raises."""
    try:
        dfd = os.open(base, _DIR_FLAGS)
    except OSError:
        return
    try:
        _prune_fd(dfd, keep)
    finally:
        os.close(dfd)


def write_workspace_paste(folder: str, filename: str, data: bytes) -> str:
    """Store a paste as ``<folder>/.mindflock_pastes/<filename>`` and prune
    that folder → the file's path.

    Race-free against the workspace's own agent: the paste folder is opened
    ``O_NOFOLLOW`` relative to ``folder`` and the file created ``O_EXCL |
    O_NOFOLLOW`` relative to it, so a planted or swapped-in symlink makes this
    raise ``OSError`` instead of writing (or pruning) outside the workspace."""
    if not filename or "/" in filename or filename in (".", ".."):
        raise OSError("bad paste name")
    wfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            os.mkdir(WORKSPACE_PASTE_DIR, 0o700, dir_fd=wfd)
        except FileExistsError:
            pass
        pfd = os.open(WORKSPACE_PASTE_DIR, _DIR_FLAGS, dir_fd=wfd)
    finally:
        os.close(wfd)
    try:
        fd = os.open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=pfd,
        )
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _prune_fd(pfd, _PASTE_KEEP)
    finally:
        os.close(pfd)
    return os.path.join(folder, WORKSPACE_PASTE_DIR, filename)


def _clear_all_pastes() -> None:
    """Server restart wipes every pasted screenshot (global dir + each known
    session workspace). Never raises."""
    srv = _server()
    for base in srv._paste_dirs():
        srv._prune_pastes(base, keep=0)


def _safe_upload_name(raw: str) -> str:
    """Reduce a client-supplied filename to a safe single path segment.

    Basename only (both separators), every character outside
    ``[A-Za-z0-9._-]`` replaced, no leading dots (hidden files / ``..``),
    capped so the stamped prefix never pushes past filesystem name limits.
    Returns "" when nothing usable survives.
    """
    name = re.split(r"[/\\]", raw or "")[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).lstrip(".")
    if len(name) > 80:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:70] + dot + ext[:9]) if dot else name[:80]
    return name
