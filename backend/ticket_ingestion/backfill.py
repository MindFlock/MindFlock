"""Backfill scanner: catch up on tickets missed during downtime.

Provider-agnostic: the scanner delegates the actual "list tickets assigned to
me since <t>" fetch to the configured :class:`TicketProvider`
(:meth:`BackfillScanner._fetch_stories`).
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from backend.ticket_ingestion.config import (
    PipelineConfig,
    TicketProviderConfig,
    source_agent_now,
)
from backend.ticket_ingestion.models import Ticket
from backend.ticket_ingestion.providers import get_provider
from backend.ticket_ingestion.state import (
    load_last_run_timestamp,
    load_pending_stories,
    load_processed_story_ids,
    record_pending_story,
    save_last_run_timestamp,
)

_logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_INITIAL_BACKOFF = 1.0
_STATE_DIR = Path(".")
# Wall-clock cap on `git ls-remote` — a stalled TCP connection (no output, no
# exit) must not wedge the scan/pipeline forever.
_LS_REMOTE_TIMEOUT = 120.0


def filter_stories_by_branches(
    stories: list[Ticket], existing_branches: set[str]
) -> list[Ticket]:
    """Drop tickets that already have a ``feature/<slug>/…`` branch on the remote.

    Provider-agnostic: keys on ``ticket.slug`` (``sc-<id>`` for Shortcut, so the
    historic ``feature/sc-<id>/`` scheme still matches exactly)."""

    def has_branch(story: Ticket) -> bool:
        prefix = f"feature/{story.slug}/"
        return any(b.startswith(prefix) for b in existing_branches)

    return [s for s in stories if not has_branch(s)]


def sort_stories_chronologically(
    stories: list[Ticket],
) -> list[Ticket]:
    return sorted(stories, key=lambda s: s.created_at)


async def _get_existing_branches(repo_url: str) -> set[str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "ls-remote",
        "--heads",
        repo_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    branches: set[str] = set()
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_LS_REMOTE_TIMEOUT
        )
    except asyncio.TimeoutError:
        _logger.warning(
            "git ls-remote %s timed out after %.0fs; treating as no branches",
            repo_url,
            _LS_REMOTE_TIMEOUT,
        )
        proc.kill()
        try:
            await proc.communicate()  # reap the killed process
        except ProcessLookupError:
            pass
        return branches
    if proc.returncode != 0:
        return branches
    for line in stdout.decode(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            branches.add(parts[1][len("refs/heads/") :])
    return branches


class BackfillScanner:
    def __init__(
        self,
        config: PipelineConfig,
        queue: asyncio.Queue,
        source: "TicketProviderConfig | None" = None,
        source_key: str | None = None,
    ) -> None:
        self.config = config
        self.queue = queue
        # Default to the primary source; multi-source orchestration passes an
        # explicit source. Each source keeps its own keyed poll checkpoint.
        self._source = source if source is not None else config.ticketing
        self._source_key = source_key or self._source.id
        self._provider = get_provider(self._source)

    async def scan(self) -> int:
        last_run = load_last_run_timestamp(_STATE_DIR, self._source_key)
        scan_started = datetime.now(timezone.utc)

        stories: list[Ticket] | None = None
        backoff = _INITIAL_BACKOFF
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                stories = await self._fetch_stories(last_run)
                break
            # aiohttp's total-timeout surfaces as asyncio.TimeoutError, the
            # most common transient failure — it must hit the backoff too.
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                _logger.warning(
                    "Backfill API error (attempt %d/%d): %s",
                    attempt,
                    _MAX_RETRIES,
                    e,
                )
                if attempt == _MAX_RETRIES:
                    _logger.error(
                        "Backfill scan failed after %d attempts; continuing without backfill",
                        _MAX_RETRIES,
                    )
                    return 0
                await asyncio.sleep(backoff)
                backoff *= 2

        if stories is None:
            return 0

        # Multi-repo ingestion: this source's tickets provision into (and are
        # branch-checked against) its own repo when set, else the global default.
        source_repo = self._source.repo_url or self.config.repo_url
        existing = await _get_existing_branches(source_repo)
        processed_ids = load_processed_story_ids(_STATE_DIR)
        # Also skip tickets already sitting in the queue (their pending marker
        # is written below) — the orchestrator re-enqueues those on startup.
        pending_ids = {e.get("story_id") for e in load_pending_stories(_STATE_DIR)}
        eligible = [
            s
            for s in filter_stories_by_branches(stories, existing)
            if s.slug not in processed_ids and s.slug not in pending_ids
        ]
        ordered = sort_stories_chronologically(eligible)

        for story in ordered:
            # Stamp the source repo so process_story / the provisioner clone the
            # right repo (empty -> global fallback downstream), and the source's
            # agent CLI so the session launches with it (empty -> the
            # [mindflock].agent / engine-default fallback downstream).
            #
            # The agent is re-read from disk rather than taken from the config
            # this scanner was built with: the pipeline loads its config once at
            # boot, so the snapshot's value is whatever was configured then, and
            # switching a source's Agent CLI in the UI looked like it was being
            # ignored until the pipeline was restarted.
            story.repo_url = self._source.repo_url
            story.agent = source_agent_now(self._source_key, self._source.agent)
            # Crash safety: persist a pending marker BEFORE the checkpoint
            # advances, so a ticket that dies in the in-memory queue is
            # re-enqueued on the next startup instead of lost forever.
            record_pending_story(_STATE_DIR, story.slug, story.id, self._source_key)
            await self.queue.put(story)

        save_last_run_timestamp(_STATE_DIR, scan_started, self._source_key)
        return len(ordered)

    async def _fetch_stories(self, since: datetime) -> list[Ticket]:
        """Tickets assigned to the configured user, changed since ``since``.

        Delegates to the active provider adapter (Shortcut / Jira / Linear /
        GitHub / Asana). Kept as a method so it stays the single mock seam the
        scanner tests patch."""
        return await self._provider.search_assigned(since)
