"""One session's shipping state, read fresh — what a caller of the commit and
push routes polls to learn whether its step has finished.

WHY THIS EXISTS. ``POST /commit`` and ``POST /push-branch`` type a shell
one-liner into the session's interactive shell and return before anything has
happened: no exit code, no completion callback (see :mod:`autopilot` for why
that is deliberate — the user watches the hooks live). The autopilot driver
copes by re-deriving the stage every pass. A caller that drives the steps
itself (the MindFlock MCP's ``ship_session``) needs the same facts as data,
and needs them precise enough to tell "my commit finished" from "nothing has
started yet":

* ``commit_rc`` / ``commit_at`` — the exit status the commit one-liner wrote,
  and when (the file's mtime). The one-liner deletes the marker FIRST and
  writes it LAST, so a marker older than the moment the caller asked is a
  previous attempt's, never this one's.
* ``committing`` — the commit lock, read through the stage pill's own
  liveness + self-heal rules (so an abandoned lock heals here exactly as it
  would on the next stage read).
* ``head_sha`` / ``upstream_sha`` / ``pushed`` — the local ``HEAD`` against
  ``refs/remotes/origin/<branch>``, which ``git push -u origin HEAD`` updates on
  success. Local refs only: no network, so polling this every few seconds is
  cheap.
* ``shell_tail`` (only when asked) — the end of the shell pane, which is where
  a failed hook or a refused push says why.

Otherwise read-only: nothing here writes, stages, commits or watches anything.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

#: Upper bound on ``tail`` lines and on the returned text.
MAX_TAIL_LINES = 200
MAX_TAIL_CHARS = 6000


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


def _upstream_sha(wt: str, branch: str) -> str:
    """``refs/remotes/origin/<branch>`` in ``wt``, or ``""`` when absent."""
    if not branch:
        return ""
    srv = _server()
    try:
        cp = srv._run_capped(
            [
                "git",
                "-C",
                wt,
                "rev-parse",
                "--verify",
                "--quiet",
                "refs/remotes/origin/" + branch,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:  # noqa: BLE001 — a probe never fails the read
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.decode("utf-8", "replace").strip()


def _commit_marker(wt: str):
    """``(rc, mtime)`` from the commit one-liner's exit-status file, or
    ``(None, None)`` when there is none (or it can't be read)."""
    path = os.path.join(wt, _server()._COMMIT_STATUS_FILE)
    try:
        at = os.path.getmtime(path)
        with open(path) as f:
            raw = f.read().strip()
    except OSError:
        return None, None
    try:
        return int(raw), at
    except ValueError:
        return None, at


def tail_text(text: str, lines: int) -> str:
    """The last ``lines`` non-blank-trailing lines of a pane capture, capped
    at :data:`MAX_TAIL_CHARS` (keeping the end)."""
    rows = (text or "").rstrip("\n").splitlines()
    while rows and not rows[-1].strip():
        rows.pop()
    out = "\n".join(rows[-max(1, min(int(lines), MAX_TAIL_LINES)) :])
    return out[-MAX_TAIL_CHARS:]


def snapshot(inst, title: str, wt: str, tail: int = 0) -> dict:
    """The ship-status document for one session (see the module docstring).
    Blocking — call it in a worker thread."""
    srv = _server()
    branch = srv._current_branch(wt) or getattr(inst, "Branch", "") or ""
    head = srv._git_head_sha(wt) or ""
    upstream = _upstream_sha(wt, branch)
    rc, at = _commit_marker(wt)
    try:
        from backend.web.core import agent_state

        committing = bool(agent_state._precommit_lock_active(inst, wt))
    except Exception:  # noqa: BLE001
        committing = False
    try:
        base = str(srv._session_base_branch(inst) or "")
    except Exception:  # noqa: BLE001
        base = ""
    beyond: Optional[int] = None
    if base:
        try:
            beyond = srv._commits_beyond_base(wt, base)
        except Exception:  # noqa: BLE001
            beyond = None
    out = {
        "title": title,
        "now": time.time(),
        "branch": branch,
        "base": base,
        "head_sha": head,
        "upstream_sha": upstream,
        "pushed": bool(head) and head == upstream,
        "dirty": bool(srv._is_dirty(wt)),
        "beyond_base": beyond,
        "committing": committing,
        "commit_rc": rc,
        "commit_at": at,
        "has_origin": bool(srv._has_origin(wt)),
        "failed_step": None,
        "failed_hook": None,
    }
    if rc not in (None, 0) and not committing:
        out["failed_step"] = srv._failed_precommit_step(title)
        out["failed_hook"] = srv._failed_precommit_hook(title)
    if tail and tail > 0:
        out["shell_tail"] = tail_text(srv._capture_shell_pane(title, 2000) or "", tail)
    return out
