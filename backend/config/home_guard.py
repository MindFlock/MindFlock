"""A hard stop between the test suite and the owner's real ``~/.mindflock``.

Every per-user store (``state.json``, ``settings.json``, ``autopilot.json``,
the team-run files, prompt queues, the mailbox, the MCP run dir, ...) resolves
its path from :func:`backend.config.config.GetConfigDir` or from a
``$MINDFLOCK_*_FILE`` / ``$MINDFLOCK_*_DIR`` override. ``tests/conftest.py``
redirects each of them — but an autouse fixture is only as good as the conftest
that loads it: a scratch suite that did ``from tests.conftest import *`` skipped
the underscore-named redirects and wrote fake runs and armed autopilot records
straight into a developer's live store.

So the stores refuse on their own. Under pytest, :func:`guard` raises
:class:`RealHomeStoreError` for any path inside the REAL user's (``pwd``, not
``$HOME``) ``.mindflock`` or ``.mindflock-assistant`` directory, whatever route
the path arrived by (an unset override, an override pointing at the real file,
a ``$HOME`` that was never redirected). Outside pytest it is the identity.

The error subclasses :class:`OSError` on purpose: the Go-parity readers
(``LoadState``, ``LoadConfig``) already turn "config dir cannot be resolved"
into their defaults, so a server imported under pytest boots on an empty state
instead of the developer's open windows — while every WRITE raises loudly.
"""

from __future__ import annotations

import os
import sys

__all__ = ["RealHomeStoreError", "guard", "real_home", "under_pytest"]

# The per-user directories the suite must never read or write.
GUARDED_DIR_NAMES = (".mindflock", ".mindflock-assistant")


class RealHomeStoreError(OSError):
    """A store path resolved into the real user's home while under pytest."""


def under_pytest() -> bool:
    """True inside a pytest process — during collection (``pytest`` imported)
    as well as during a test (``PYTEST_CURRENT_TEST`` set)."""
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def real_home() -> str:
    """The real user's home from the password database — deliberately NOT
    ``$HOME``, which is exactly what a test redirects (or forgets to)."""
    try:
        import pwd

        return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:  # noqa: BLE001 — no pwd entry: fall back to ~ expansion
        return os.path.realpath(os.path.expanduser("~"))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def guard(path: str, what: str = "store") -> str:
    """Return ``path`` unchanged, or raise under pytest when it lies inside the
    real user's ``~/.mindflock`` / ``~/.mindflock-assistant``."""
    if not path or not under_pytest():
        return path
    home = real_home()
    resolved = os.path.realpath(os.path.expanduser(str(path)))
    for name in GUARDED_DIR_NAMES:
        root = os.path.realpath(os.path.join(home, name))
        if _inside(resolved, root):
            raise RealHomeStoreError(
                "refusing to touch the real {} ({}) under pytest: redirect it "
                "(tests/conftest.py sets $HOME-independent MINDFLOCK_*_FILE / "
                "MINDFLOCK_*_DIR overrides; a scratch conftest that does "
                "`from tests.conftest import *` skips them)".format(what, resolved)
            )
    return path


def guard_ledger_dir(path, what: str = "ticket ledger"):
    """:func:`guard` for the ingestion pipeline's ``<repo root>/state.json``
    ledger, which lives in the app CHECKOUT rather than ``~/.mindflock``: under
    pytest, also refuse any directory that contains this source tree (the
    checkout itself, or the ``config.toml`` ancestor a worktree resolves to —
    the owner's real pipeline ledger) and any OTHER MindFlock checkout (one
    holding ``backend/ticket_ingestion``): a ``$MINDFLOCK_REPO_ROOT`` the live
    server exports into agent shells names the owner's main checkout while
    the suite runs from a sibling worktree."""
    if not path or not under_pytest():
        return path
    guard(str(path), what)
    resolved = os.path.realpath(os.path.expanduser(str(path)))
    here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    if resolved != os.sep and (
        _inside(here, resolved)
        or os.path.isdir(os.path.join(resolved, "backend", "ticket_ingestion"))
    ):
        raise RealHomeStoreError(
            "refusing to touch the real {} under {} under pytest: point the "
            "module's _REPO_ROOT at a tmp dir (tests/conftest.py does for "
            "ticket_start)".format(what, resolved)
        )
    return path
