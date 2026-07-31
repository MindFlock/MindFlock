"""Re-exec the server in place, and the one case that does it by itself.

``POST /api/server/restart`` (Settings → Mobile / Advanced) exists because the
serve mode — which interface uvicorn binds — is fixed at process start, so
flipping the Settings → Mobile toggle can only take effect on the next boot.
Re-execing is safe: the actual work lives outside this process (agent sessions
are tmux sessions, ingestion is its own process, state is on disk), and every
client already retries until the server answers again.

:func:`auto_restart_for_tailscale` is that same re-exec, self-service: when the
persisted mode says *tailscale* but this process is bound to 127.0.0.1, the
setting is simply not in effect, and the fix is entirely mechanical. So we do
it instead of asking. The one thing it must not do is loop — a restart that
doesn't change the outcome would restart forever — so attempts are capped at
:data:`MAX_TAILSCALE_ATTEMPTS`, counted **in the environment**: each attempt is
a new process image, so an in-memory counter would reset every time and never
reach its own limit. ``execv`` keeps the environment, so the count survives
exactly as far as the chain of restarts it is meant to bound, and a genuinely
fresh start (or an explicit user toggle, via :func:`reset_tailscale_attempts`)
begins from zero.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from backend import log
from backend.web.core.mobile_access import _local_only_mode

#: Mode words the launcher accepts positionally (``mindflock serve local``).
#: Stripped from argv on re-exec so the fresh process falls through to the
#: *persisted* general.serve_mode instead of resurrecting the mode this process
#: happened to boot with.
_MODE_TOKENS = ("local", "localhost", "tailscale", "ts", "all")

#: How many times a boot may re-exec itself chasing tailscale mode before it
#: gives up and stays local. Three: enough to ride out a transient failure
#: (a port still in TIME_WAIT), few enough that a permanent one costs seconds.
MAX_TAILSCALE_ATTEMPTS = 3

#: Where the attempt count rides across ``execv``.
_ATTEMPT_ENV = "MINDFLOCK_TAILSCALE_RESTARTS"

#: Set by ``run.py`` immediately before it hands control to uvicorn — the one
#: place that knows this process exists to *serve*. The automatic restart
#: refuses to act without it: importing the app (a test, a tool, a REPL) must
#: never end with that process replaced by a server.
_SERVING = False


def mark_serving() -> None:
    """Declare this process the real server (called by ``run.main``)."""
    global _SERVING
    _SERVING = True


def _under_pytest() -> bool:
    """True inside a test run. ``execv`` there would replace the test runner
    with a server — a guard worth having in front of every call, because the
    blast radius is the whole suite rather than one failed assertion."""
    return "pytest" in sys.modules


def reexec_soon(delay: float = 0.5) -> None:
    """Re-exec this process after ``delay`` seconds (never returns to caller).

    The delay lets the HTTP response that asked for the restart flush to the
    client first — without it the caller sees a dropped connection instead of
    the ``{"ok": true}`` that tells it to start polling for the server's return.
    """
    if _under_pytest():
        return
    os.environ.pop("CS_WEB_MODE", None)
    argv = [a for a in sys.argv if a.strip().lower() not in _MODE_TOKENS]

    def _reexec() -> None:
        time.sleep(delay)
        os.execv(sys.executable, [sys.executable] + argv)

    threading.Thread(target=_reexec, daemon=True).start()


def reset_tailscale_attempts() -> None:
    """Forget previous auto-restart attempts.

    Called when the user turns tailscale mode on by hand: an explicit choice is
    a fresh intent, and shouldn't inherit the budget a previous one spent.
    """
    os.environ.pop(_ATTEMPT_ENV, None)


def _serve_mode_setting() -> str:
    """The persisted Settings → Mobile mode, normalized. Never raises."""
    try:
        from backend.config.settings import load_settings

        return (load_settings().general.serve_mode or "").strip().lower()
    except Exception:  # noqa: BLE001 — an unreadable store means "don't act"
        return ""


def auto_restart_for_tailscale(delay: float = 1.0) -> bool:
    """Re-exec when tailscale mode is on but this process is bound locally.

    Returns whether a restart was scheduled — False covers every ordinary case
    (mode already in effect, mode not requested, budget spent), so callers can
    use it to decide whether the process has a future worth doing more work in.
    ``delay`` is the same "let the response out first" grace :func:`reexec_soon`
    takes; the default suits the startup path, where nobody is waiting on a
    response but the server has just begun serving.

    Only fires when we *know* the bind is local *and* that this process is the
    server: ``_local_only_mode`` reads the mode the launcher resolved and
    exported, and :func:`mark_serving` is set by ``run.main`` alone. Anything
    else that merely imported the app — a test client, a script — is left
    running rather than replaced on a guess about how it was launched.
    """
    if not _SERVING or _under_pytest():
        return False
    if _serve_mode_setting() != "tailscale" or not _local_only_mode():
        return False
    raw = os.environ.get(_ATTEMPT_ENV, "")
    attempts = int(raw) if raw.isdigit() else 0
    if attempts >= MAX_TAILSCALE_ATTEMPTS:
        _say(
            "Tailscale mode is on but this server is still bound to 127.0.0.1 "
            "after %d restart attempts — staying local. Start it with "
            "`mindflock serve tailscale` to see why." % attempts
        )
        return False
    os.environ[_ATTEMPT_ENV] = str(attempts + 1)
    _say(
        "Tailscale mode is on but this server is bound to 127.0.0.1 — "
        "restarting to apply it (attempt %d of %d)."
        % (attempts + 1, MAX_TAILSCALE_ATTEMPTS)
    )
    reexec_soon(delay=delay)
    return True


def _say(msg: str) -> None:
    """Console + log. The console is where someone watching a `serve` run finds
    out why it just restarted itself; the log is where they find out later."""
    print("  " + msg, flush=True)
    if log.ErrorLog is not None:
        try:
            log.ErrorLog.Printf("%s", msg)
        except Exception:  # noqa: BLE001
            pass
