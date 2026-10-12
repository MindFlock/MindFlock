"""Entry point for `python -m backend.ticket_ingestion`."""

import asyncio
import datetime as _dt
import fcntl
import json
import logging
import os
import re
import sys

from backend.fsutil import atomic_write_json
from backend.ticket_ingestion.config import ConfigError, load_config
from backend.ticket_ingestion.logging_config import setup_logging
from backend.ticket_ingestion.orchestrator import PipelineOrchestrator

_logger = logging.getLogger(__name__)

# Held for the process lifetime so the advisory lock stays acquired. Module-level
# so it is never garbage-collected (which would release the flock).
_LOCK_HANDLE = None

#: Set by the web server on the child it starts (see the ticket-ingestion
#: addon): a pipeline the server started is the server's to restart after an
#: engine update; a standalone run is left alone.
OWNER_ENV = "MINDFLOCK_PIPELINE_OWNER"


def _lock_meta() -> dict:
    """What the lock file records besides the PID: the engine build this
    pipeline runs (version + installed commit) and who started it. The server
    compares it with its own after an update and restarts a stale child —
    otherwise ticket polling, PR review and the refresher keep running the
    previous version indefinitely."""
    meta = {"version": "", "commit": "", "owner": os.environ.get(OWNER_ENV, "")}
    try:
        from backend import __version__

        meta["version"] = str(__version__)
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.web.core.self_update import installed_commit

        meta["commit"] = installed_commit()
    except Exception:  # noqa: BLE001 — a lock line never stops the pipeline
        pass
    return meta


#: Written next to the lock when this process exits over a configuration
#: error or an unhandled crash: ``{pid, at, kind, message, fields}``. The web
#: server reads it to say WHY its child is gone (``/api/mindflock/status`` →
#: ``last_error`` / ``exit_reason``) instead of "starting, or exited".
EXIT_RECORD = ".mindflock-pipeline-exit.json"

_FIELD_LINE_RE = re.compile(r"^\s*-\s*(\S+)")


def _write_exit_record(kind: str, message: str) -> None:
    """Best-effort: record why this process is exiting (see
    :data:`EXIT_RECORD`). ``fields`` are the config paths a ConfigError
    lists (one per ``  - path (hint)`` line)."""
    fields = []
    if kind == "config":
        for line in message.splitlines():
            m = _FIELD_LINE_RE.match(line)
            if m:
                fields.append(m.group(1))
    try:
        atomic_write_json(
            os.path.join(os.getcwd(), EXIT_RECORD),
            {
                "pid": os.getpid(),
                "at": _dt.datetime.now().astimezone().isoformat(),
                "kind": kind,
                "message": message,
                "fields": fields,
            },
            indent=None,
            newline=False,
            ensure_ascii=True,
        )
    except OSError:
        pass


def _acquire_singleton_lock() -> bool:
    """Take an exclusive advisory lock on a per-repo lockfile.

    Two pipeline instances running against the same repo both poll GitHub, both
    see the same PR as unprocessed, and provision into the same
    ``workspaces/pr-<n>`` directory at once — which corrupts each other's git
    clone and collides on the shared ``mindflock_pr-<n>`` tmux session name.
    The lock (keyed on the working directory, the repo root for both the
    standalone and the backend.web-launched pipeline) ensures only one runs at a time.
    Returns False if another instance already holds it.
    """
    global _LOCK_HANDLE
    lock_path = os.path.join(os.getcwd(), ".mindflock-pipeline.lock")
    # Open WITHOUT truncating ("a+", not "w"): a losing process must not wipe the
    # winner's PID from the file before its flock check fails. Only the winner
    # (which acquires the lock) rewrites the PID.
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fh.seek(0)
    fh.truncate(0)
    # Line 1 the PID (all an older server reads), line 2 the build it runs.
    fh.write("%d\n%s\n" % (os.getpid(), json.dumps(_lock_meta())))
    fh.flush()
    _LOCK_HANDLE = fh
    return True


def main() -> None:
    try:
        config = load_config()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        _write_exit_record("config", str(e))
        sys.exit(1)

    setup_logging(config)

    if not _acquire_singleton_lock():
        msg = (
            "Another MindFlock pipeline is already running for this repo "
            f"(lock: {os.path.join(os.getcwd(), '.mindflock-pipeline.lock')}). Exiting."
        )
        _logger.error(msg)
        print(msg, file=sys.stderr)
        # Exit 0: an intentional no-op, not a crash, so the backend.web's MindFlock
        # controller doesn't surface it as a failure.
        sys.exit(0)

    orchestrator = PipelineOrchestrator(config)

    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
    except Exception as e:  # noqa: BLE001 — say why before the traceback
        _write_exit_record("crash", "%s: %s" % (type(e).__name__, e))
        raise


if __name__ == "__main__":
    main()
