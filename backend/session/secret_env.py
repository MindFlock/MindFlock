"""Hand a session's *credentials* to its shell without putting them in argv.

The problem this exists for: every launch path builds one shell string and
hands it to ``tmux new-session … sh -c <string>``. Non-secret values riding
along in that string are harmless — a port number, a cache tag. An auth
profile's API key is not. A process's argv is world-readable on Linux
(``/proc/<pid>/cmdline``, hence ``ps``), and there are two exposures, not one:

* the ``tmux`` client's own argv, for as long as the client runs (brief), and
* the ``env KEY=… <cmd>`` child, for the entire life of the session (not brief).

``tmux set-environment -t <session> KEY value`` has the same shape.

So secrets go to a file instead: mode 0600, under the app's own run dir, named
for the tmux session. The launch string carries ``. <path>`` — a filename, not
a credential — and the values reach the process through the shell's own
environment. What lands in ``ps`` is the path.

This is not a claim of secrecy against a root user or against anyone who can
read the settings store (which holds the same keys, also 0600). It closes the
gap that mattered: on a shared machine, *any* other local account could read a
key out of the process table without touching a single file of ours.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

#: Substrings that make an env var name a credential. Deliberately broad — a
#: false positive costs one file write, a false negative puts a key in ``ps``.
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

#: …minus the names that merely *look* like credentials. ``TESTMON_ENV`` and
#: the port block never match; these are the ones that do and must not, because
#: routing them through a file for nothing would only add a file to clean up.
_NOT_SECRET = frozenset(
    {
        # A documented literal placeholder LM Studio ignores; see
        # backend/providers/local_models.py.
        "LM_STUDIO_API_KEY",
    }
)


def is_secret(name: str) -> bool:
    """Whether ``name`` names a credential rather than a setting."""
    n = (name or "").upper()
    if n in _NOT_SECRET:
        return False
    return any(h in n for h in _SECRET_HINTS)


def split(env: Optional[Mapping[str, str]]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """``(plain, secret)`` — the halves that may and may not reach argv."""
    plain: Dict[str, str] = {}
    secret: Dict[str, str] = {}
    for k, v in (env or {}).items():
        (secret if is_secret(k) else plain)[str(k)] = str(v)
    return plain, secret


def run_dir() -> Path:
    """Directory holding the per-session env files.

    ``$MINDFLOCK_RUN_DIR`` overrides (tests point it at a tmp dir); otherwise
    ``~/.mindflock/run``, so an uninstall ``--purge`` sweeps it with everything
    else and it is never inside a git worktree.
    """
    env = os.environ.get("MINDFLOCK_RUN_DIR")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".mindflock" / "run"


def _path(session_name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_name or "")
    return run_dir() / (safe + ".env")


def write(session_name: str, secret: Optional[Mapping[str, str]]) -> str:
    """Persist ``secret`` as a sourceable file for ``session_name``.

    Returns the path, or ``""`` when there is nothing to write (in which case
    any previous file is removed, so a session that stops using a profile stops
    carrying its key). Never raises: a launch must not fail over this — the
    caller falls back to the plain path and the session still starts.
    """
    if not session_name:
        return ""
    path = _path(session_name)
    if not secret:
        clear(session_name)
        return ""
    try:
        d = run_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        body = "".join(
            "export %s=%s\n" % (k, shlex.quote(str(v)))
            for k, v in sorted(secret.items())
        )
        # Create 0600 from the start: writing then chmod-ing leaves a window in
        # which the file is world-readable.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body.encode("utf-8"))
        finally:
            os.close(fd)
        return str(path)
    except Exception:  # noqa: BLE001 — never block a launch
        return ""


def source_prefix(path: str) -> str:
    """Shell that loads ``path`` if it is there, or ``""`` for no path.

    Tolerates the file having been swept (``-f`` guard) so a stale launcher
    never dies on a missing include — it just starts without the credential,
    which is the same outcome as never having had one.
    """
    if not path:
        return ""
    return ". %s 2>/dev/null || true\n" % shlex.quote(path)


def clear(session_name: str) -> None:
    """Remove a session's env file (best-effort, never raises)."""
    try:
        _path(session_name).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
