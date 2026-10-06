"""Server-side refusals for shared-folder (peer-link) sessions.

A ``PeerShare`` session's folder holds a remote collaborator's agent's work. It
only ever runs sandboxed, so every host-side feature that would execute that
folder's content, publish it, or tie the session into the local flock is
refused with a 409 (see docs/peer-link.md, "Engine integration"):

* ship, push, PR, merge, commit, autopilot / lanes, team runs;
* spawning from it (``parent`` = a shared session) and re-parenting it;
* worktree setup / check scripts, the IDE, the shell pane, copies, rename,
  test plans, the destructive cleanup (unshare deletes the folder instead).

The engine itself (``instance.new_instance`` / ``_peer_guard_launch``) also
refuses any ordinary session on a path under the peer root, so a creator that
skips :func:`path_refusal` still fails closed.
"""

from __future__ import annotations

from typing import Optional

from fastapi.responses import JSONResponse

__all__ = [
    "is_peer_session",
    "refusal",
    "title_refusal",
    "path_refusal",
    "PEER_ROOT_REFUSED",
]

PEER_ROOT_REFUSED = (
    "that folder belongs to a peer link — shared folders only run as their "
    "link's sandboxed session"
)


def is_peer_session(inst) -> bool:
    return bool(getattr(inst, "PeerShare", "") or "") if inst is not None else False


def refusal(inst, what: str) -> Optional[JSONResponse]:
    """409 when ``inst`` is a shared-folder session, else None."""
    if not is_peer_session(inst):
        return None
    return JSONResponse(
        {
            "error": "%s is not available for a shared-folder (peer) session" % what,
            "peer_share": True,
        },
        status_code=409,
    )


def title_refusal(title: str, what: str) -> Optional[JSONResponse]:
    """:func:`refusal` for a session named by title (None when unknown)."""
    from backend.web import server

    return refusal(server.ENGINE.instances.get(title or ""), what)


def path_refusal(path: str) -> Optional[JSONResponse]:
    """409 when ``path`` (resolved — symlinks and ``..`` included) is under the
    peer root. Undecidable counts as inside (fail closed)."""
    if not path:
        return None
    try:
        from backend.peer import paths as _peer_paths

        inside = _peer_paths.is_inside_peer_root(str(path))
    except Exception:  # noqa: BLE001
        inside = True
    if not inside:
        return None
    return JSONResponse({"error": PEER_ROOT_REFUSED}, status_code=409)
