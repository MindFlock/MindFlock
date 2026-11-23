"""A per-session key binding a session's own MindFlock MCP to the session
routes that act FOR that session (the hub's put / take / remove-own;
``backend/web/routes/hub.py``).

Why. Every local client — the app, the CLI, each session's MCP — reaches the
server with the same access token, so a route named by a session title
(``/api/instances/{title}/hub/take``) cannot tell session A's agent from
session B's: A could act as B by writing B's title in the URL. The key closes
that: ``key_for(title)`` is an HMAC of the title under a server secret
(``<mindflock home>/session_key.secret``, 0600, made on first use). The
launcher hands each managed session's MCP ITS key in the MCP's environment
(``MINDFLOCK_SESSION_KEY``, :mod:`backend.providers.mcp_attach`); the MCP
sends it with every session route call; the route checks it against the
title in the URL (:func:`verify`). The peer-mode MCP of a shared-folder
session is never given one.

What it does NOT do. Every session runs as the same OS user on the same
filesystem. A process that deliberately reads another session's MCP
configuration (its run file, ``/proc/<pid>/environ``) or the secret file
itself can still act as that session — nothing short of separate OS users
(or the peer sandbox) stops that. The key stops a session's agent from
acting for another by naming it: a confused or prompt-injected agent that
calls the HTTP API with someone else's title is refused.

A renamed session keeps its old key until its next launch; its MCP keeps the
old title too, so the two stay consistent (a relaunch rewrites both).

Stdlib only, no ``backend`` imports beyond the config dir: the launcher
(providers) and the server both use it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading

ENV = "MINDFLOCK_SESSION_KEY"
FILE = "session_key.secret"
_LOCK = threading.Lock()
_CACHE: dict = {}


def _home() -> str:
    from backend.config import config

    return config.GetConfigDir()


def _secret() -> bytes:
    path = os.path.join(_home(), FILE)
    with _LOCK:
        got = _CACHE.get(path)
        if got:
            return got
        try:
            with open(path, "rb") as f:
                raw = f.read().strip()
        except FileNotFoundError:
            raw = b""
        if len(raw) < 32:
            raw = secrets.token_hex(32).encode()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".%d.tmp" % os.getpid()
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                # Never replace a secret another process wrote meanwhile.
                os.link(tmp, path)
            except FileExistsError:
                with open(path, "rb") as f:
                    raw = f.read().strip()
            except OSError:
                os.replace(tmp, path)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        _CACHE[path] = raw
        return raw


def key_for(title: str) -> str:
    """The key of the session called ``title`` (hex)."""
    return hmac.new(
        _secret(), ("session:" + (title or "")).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify(title: str, key) -> bool:
    """Whether ``key`` is ``title``'s key (constant time)."""
    if not isinstance(key, str) or not key or not title:
        return False
    return hmac.compare_digest(key_for(title), key.strip())


def reset_cache() -> None:
    """Forget the cached secret (tests that move the home)."""
    with _LOCK:
        _CACHE.clear()
