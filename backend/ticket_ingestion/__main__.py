"""Entry point for `python -m backend.ticket_ingestion`."""

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import sys

from backend import compat
from backend.fsutil import atomic_write_json
from backend.ticket_ingestion.config import ConfigError, load_config
from backend.ticket_ingestion.logging_config import setup_logging
from backend.ticket_ingestion.orchestrator import PipelineOrchestrator

_logger = logging.getLogger(__name__)

# Held for the process lifetime so the advisory locks stay acquired (a list:
# the MindFlock-home lock, then the legacy per-root one). Module-level so it
# is never garbage-collected (which would release the flock).
_LOCK_HANDLE = None

#: Set by the web server on the child it starts (see the ticket-ingestion
#: addon): a pipeline the server started is the server's to restart after an
#: engine update; a standalone run is left alone.
OWNER_ENV = "MINDFLOCK_PIPELINE_OWNER"
#: The PID of the server that started this pipeline (same literal in the
#: ticket-ingestion addon), recorded in the lock as ``server_pid``.
SERVER_PID_ENV = "MINDFLOCK_PIPELINE_SERVER_PID"


def _env_int(name: str) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or 0)
    except ValueError:
        return 0


def _lock_meta() -> dict:
    """What the lock file records besides the PID: the engine build this
    pipeline runs (version + installed commit) and who started it. The server
    compares it with its own after an update and restarts a stale child —
    otherwise ticket polling, PR review and the refresher keep running the
    previous version indefinitely."""
    meta = {
        "version": "",
        "commit": "",
        "owner": os.environ.get(OWNER_ENV, ""),
        # Which server started it (pid + port): a second server sharing this
        # MindFlock home reads "another server's pipeline", never its own
        # stale child to restart (see singleton.py).
        "server_pid": _env_int(SERVER_PID_ENV),
        "server_port": _env_int("MINDFLOCK_SERVER_PORT"),
        # Where its ledger / activity beacon / log live (its cwd).
        "root": os.getcwd(),
    }
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


#: Written in the cwd (the repo root, beside the legacy per-root lock) when
#: this process exits over a configuration error or an unhandled crash: ``{pid, at, kind, message, fields}``. The web
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
    """Take the pipeline's singleton lock — one pipeline per MindFlock home.

    Two pipelines over one engine state both poll the trackers and GitHub,
    both see the same ticket / PR as new, and race to launch it: provisioning
    into the same workspace and branch, and the loser's failure path tearing
    down what the winner armed. The lock used to be keyed on the cwd (each
    server's repo root), so two servers sharing ``~/.mindflock`` from
    different directories ran two pipelines (2026-11-13). It is now keyed on
    the MindFlock home (:func:`singleton.lock_path`); the old per-root lock
    is taken as well, so an older-build pipeline still running in this root
    excludes this one until it is restarted. Returns False if another
    pipeline holds either.
    """
    global _LOCK_HANDLE
    from backend.ticket_ingestion import singleton

    # Line 1 the PID (all an older server reads), line 2 the build it runs.
    text = "%d\n%s\n" % (os.getpid(), json.dumps(_lock_meta()))
    home = singleton.lock_path(os.getcwd())
    legacy = singleton.legacy_lock_path(os.getcwd())
    held = singleton.take(home, text)
    if held is None:
        return False
    handles = [held]
    if legacy.resolve() != home.resolve():
        other = singleton.take(legacy, text)
        if other is None:
            held.close()
            return False
        handles.append(other)
    _LOCK_HANDLE = handles
    return True


def _lock_holder_text() -> str:
    """Who holds the lock this pipeline lost, for its exit line."""
    from backend.ticket_ingestion import singleton

    for path in (
        singleton.lock_path(os.getcwd()),
        singleton.legacy_lock_path(os.getcwd()),
    ):
        pid, meta = singleton.probe(path)
        if pid is None:
            continue
        who = "pid %s" % pid
        port = (meta or {}).get("server_port")
        if port:
            who += ", started by the server on port %s" % port
        return "%s (lock: %s)" % (who, path)
    return "lock: %s" % singleton.lock_path(os.getcwd())


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
            "Another MindFlock pipeline is already running for this MindFlock "
            "home — %s. Exiting." % _lock_holder_text()
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
