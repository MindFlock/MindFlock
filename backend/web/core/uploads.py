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
            dirs.append(os.path.join(folder, ".mindflock_pastes"))
    return dirs


def _prune_pastes(base: str, keep: int = _PASTE_KEEP) -> None:
    """Delete all but the ``keep`` newest ``paste-*`` files in ``base``.

    Only files this endpoint itself named (``paste-<stamp>-<hex>.<ext>``) are
    ever touched, so a user file that wandered into the directory is safe.
    Never raises."""
    try:
        names = os.listdir(base)
    except OSError:
        return
    stamped = []
    for n in names:
        if not n.startswith("paste-"):
            continue
        p = os.path.join(base, n)
        try:
            stamped.append((os.path.getmtime(p), p))
        except OSError:
            continue
    victims = sorted(stamped)[:-keep] if keep > 0 else sorted(stamped)
    for _, p in victims:
        try:
            os.remove(p)
        except OSError:
            pass


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
