"""Where peer-link state lives.

Everything sits under one root, ``~/.mindflock/peer`` (``$MINDFLOCK_PEER_HOME``
overrides; tests point it at ``tmp_path``)::

    peer/                       0700
      identity/                 0700  ed25519.key (0600), cert.pem (0644)
      links.json                0600  persisted links (store.py)
      shares/<share_id>/        0700  ONE shared folder's whole blast radius
        work/                   the folder both agents work in (rw in sandbox)
        work/.git               gitfile -> ../repo.git (read-only in sandbox)
        repo.git/               trusted git dir (read-only in sandbox)
        home/                   the sandboxed agent's $HOME (rw in sandbox)
        run/                    sockets + mcp.json + bridge.py (ro in sandbox,
                                sockets usable)

Every directory is created 0700 and re-chmodded if it already exists, so a
loose umask never widens it.
"""

from __future__ import annotations

import contextlib
import os
import re

__all__ = [
    "peer_root",
    "identity_dir",
    "links_file",
    "shares_dir",
    "share_root",
    "share_paths",
    "is_inside_peer_root",
    "ensure_dir",
    "SHARE_ID_RE",
    "unix_addr",
]

# sockaddr_un.sun_path is 108 bytes on Linux (104 on macOS) including the NUL.
_SUN_PATH_SAFE = 100

# share ids and link ids are lowercase hex minted by secrets.token_hex.
SHARE_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")


def ensure_dir(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def peer_root() -> str:
    env = os.environ.get("MINDFLOCK_PEER_HOME", "").strip()
    if env:
        return os.path.realpath(env)
    from backend.config.config import GetConfigDir

    return os.path.realpath(os.path.join(GetConfigDir(), "peer"))


def identity_dir() -> str:
    return os.path.join(peer_root(), "identity")


def links_file() -> str:
    return os.path.join(peer_root(), "links.json")


def shares_dir() -> str:
    return os.path.join(peer_root(), "shares")


def share_root(share_id: str) -> str:
    if not SHARE_ID_RE.match(share_id or ""):
        raise ValueError("bad share id")
    return os.path.join(shares_dir(), share_id)


def share_paths(share_id: str) -> dict:
    root = share_root(share_id)
    return {
        "root": root,
        "work": os.path.join(root, "work"),
        "gitdir": os.path.join(root, "repo.git"),
        "home": os.path.join(root, "home"),
        "run": os.path.join(root, "run"),
    }


def is_inside_peer_root(path: str) -> bool:
    """True when ``path`` (resolved) is the peer root or anything under it.
    Used to refuse ordinary (unsandboxed) sessions on a shared folder."""
    try:
        root = peer_root()
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return True  # fail closed
    return real == root or real.startswith(root + os.sep)


@contextlib.contextmanager
def unix_addr(path: str):
    """Yield an address for ``bind``/``connect`` that reaches ``path`` even
    when it is longer than ``sun_path`` allows (a deep ``$HOME`` puts
    ``shares/<id>/run/agent.sock`` past 108 bytes). Short paths are used
    as-is; long ones go through ``/proc/self/fd/<dirfd>/<name>``, which the
    kernel resolves to the same directory entry. The dir fd is O_PATH and
    closed on exit, so keep the context open while binding or connecting."""
    if len(os.fsencode(path)) <= _SUN_PATH_SAFE:
        yield path
        return
    fd = os.open(
        os.path.dirname(path) or ".", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
    )
    try:
        yield "/proc/self/fd/%d/%s" % (fd, os.path.basename(path))
    finally:
        os.close(fd)
