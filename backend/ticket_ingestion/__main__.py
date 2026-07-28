"""Entry point for `python -m backend.ticket_ingestion`."""

import asyncio
import fcntl
import logging
import os
import sys

from backend.ticket_ingestion.config import ConfigError, load_config
from backend.ticket_ingestion.logging_config import setup_logging
from backend.ticket_ingestion.orchestrator import PipelineOrchestrator

_logger = logging.getLogger(__name__)

# Held for the process lifetime so the advisory lock stays acquired. Module-level
# so it is never garbage-collected (which would release the flock).
_LOCK_HANDLE = None


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
    fh.write(str(os.getpid()))
    fh.flush()
    _LOCK_HANDLE = fh
    return True


def main() -> None:
    try:
        config = load_config()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
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


if __name__ == "__main__":
    main()
