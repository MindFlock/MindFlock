"""Pipeline orchestrator: wires components together and drains the queue."""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from backend.ticket_ingestion.backfill import BackfillScanner
from backend.ticket_ingestion.providers import get_provider
from backend.ticket_ingestion.clarification import InteractiveClarificationHandler
from backend.ticket_ingestion.claude_runner import ClaudeCodeRunner
from backend.ticket_ingestion.session_runner import SessionRunner, engine_bridge_error
from backend.ticket_ingestion.config import (
    PipelineConfig,
    agent_now,
    source_agent_now,
)
from backend.ticket_ingestion.filter import AssigneeFilter
from backend.ticket_ingestion.issue_monitor import (
    IssueCommentsFetchError,
    IssueMonitor,
    issue_to_ticket,
)
from backend.ticket_ingestion.models import (
    Issue,
    ProcessedIssue,
    ProcessedPR,
    ProcessingRecord,
    Ticket,
    WebhookEvent,
)
from backend.ticket_ingestion.pr_comments import (
    PRCommentsFetchError,
    fetch_actionable_comments,
)
from backend.ticket_ingestion.pr_monitor import PRMonitor
from backend.ticket_ingestion.pr_provisioner import PRProvisioner
from backend.ticket_ingestion.pr_runner import PRClaudeRunner
from backend.ticket_ingestion.provisioner import EnvironmentProvisioner
from backend.ticket_ingestion.state import (
    clear_issue_attempts,
    clear_pr_attempts,
    load_pending_stories,
    load_processed_story_ids,
    reap_stale_in_flight,
    record_issue_attempt,
    record_pr_attempt,
    record_processed_issue,
    record_processed_pr,
    record_processed_story,
    remove_pending_story,
    update_processed_story,
)
from backend.ticket_ingestion.cache_refresher import CacheRefresher
from backend.ticket_ingestion.validator import TicketValidator
from backend.ticket_ingestion.workspace_cleanup import prune_stale_workspaces

_logger = logging.getLogger(__name__)

_STATE_DIR = Path(".")
# Activity beacon for the web UI's sidebar bars: distinguishes "running but
# idle" (waiting for work) from "actively handling a ticket/PR". Lives next to
# the singleton lock in the repo root; read by the backend.web ingestion addon.
_ACTIVITY_FILE = ".mindflock-pipeline-activity.json"
# Give up on a PR whose provisioning/launch keeps failing after this many
# polls (it is then recorded processed-as-failed instead of being re-cloned
# on every poll forever).
_PR_MAX_ATTEMPTS = 3
# Same retry cap for the issue-handling loop.
_ISSUE_MAX_ATTEMPTS = 3


def _tmux_session_alive(slug: str) -> bool | None:
    """Best-effort tmux liveness for a story's session.

    A story session is named either ``<slug>`` (standalone tmux launch, see
    ``claude_runner._tmux_session_name``) or ``mindflock_<slug>`` (engine
    mode, see ``session_runner``). ``=`` forces an exact tmux name match.
    Returns None when tmux can't be probed (missing/timeout) so the reaper
    falls back to its conservative age threshold.
    """
    for name in (slug, f"mindflock_{slug}"):
        try:
            proc = subprocess.run(
                ["tmux", "has-session", "-t", f"={name}"],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode == 0:
            return True
    return False


class PipelineOrchestrator:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self._queue: asyncio.Queue = asyncio.Queue()
        self._provider = get_provider(config.ticketing)
        # One scanner per configured ticketing source, each with its own keyed
        # poll checkpoint so they don't clobber each other.
        sources = config.ticketing_sources or [config.ticketing]
        self._scanners = [
            BackfillScanner(config, self._queue, src, source_key=src.id)
            for src in sources
        ]
        # Defensive net over every source's server-side "assigned to me" search:
        # accept a ticket if it's assigned to ANY configured identity.
        member_ids = [s.member_id for s in sources if s.member_id]
        self._assignee_filter = AssigneeFilter(member_ids)
        self._validator = TicketValidator(config)
        self._provisioner = EnvironmentProvisioner(config)
        self._claude_runner = ClaudeCodeRunner(config)
        # Engine mode (the default) hands story sessions to the MindFlock engine,
        # so a ticket becomes a real app session — worktree + branch + seeded
        # agent, visible in the grid with the stage badge and the guided
        # commit → push → PR bar — instead of this package's own provisioner +
        # runner, which only leaves a detached tmux session and an OS terminal
        # tab. The bridge is in-process (no server to reach), so the only reason
        # to fall back is an environment where the engine half of the package
        # does not import; say so loudly, because a silent downgrade is exactly
        # what made a connected tracker look like it shipped terminal tabs.
        self._cs_runner: SessionRunner | None = None
        if config.engine and config.engine.enabled:
            reason = engine_bridge_error()
            if reason is None:
                self._cs_runner = SessionRunner(config)
            else:
                _logger.warning(
                    "Engine mode is enabled but the MindFlock engine bridge is "
                    "unavailable (%s); falling back to the standalone launcher — "
                    "tickets will get a detached tmux session and an OS terminal "
                    "tab, NOT an app session with the guided PR bar.",
                    reason,
                )
        self._clarification_handler = InteractiveClarificationHandler(config)
        self._pr_monitor = PRMonitor(config.github) if config.github else None
        self._issue_monitor = IssueMonitor(config.github) if config.github else None
        self._pr_provisioner = PRProvisioner(config)
        # PR review has no ticketing source of its own, so it runs PR review's
        # OWN agent ([github].agent) before the ingestion-wide fallback — the
        # same chain the engine path uses, so the two runners can't disagree
        # about which CLI reviews a PR. The agent is refreshed at launch (see
        # _review_pr) because this snapshot is taken once per process.
        self._pr_runner = PRClaudeRunner(agent=config.pr_agent())
        # Counts of in-flight work per kind, mirrored to the activity beacon.
        self._busy: dict[str, int] = {"ticket": 0, "pr": 0, "issue": 0}

    def _write_activity(self) -> None:
        """Mirror the busy counters to the beacon file (atomic replace so the
        web addon never reads a torn file). Best-effort: the beacon must never
        break ticket/PR processing."""
        path = _STATE_DIR / _ACTIVITY_FILE
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "ticket_busy": self._busy["ticket"],
                        "pr_busy": self._busy["pr"],
                        "issue_busy": self._busy["issue"],
                        "updated": datetime.now(timezone.utc).isoformat(),
                    }
                )
            )
            tmp.replace(path)
        except OSError:
            pass

    def _mark_busy(self, kind: str, delta: int) -> None:
        self._busy[kind] = max(0, self._busy[kind] + delta)
        self._write_activity()

    async def run(self) -> None:
        _logger.info(
            "Pipeline starting up (tickets %s, PR review %s, issue handling %s).",
            "on" if self.config.tickets_enabled else "off",
            (
                "on"
                if self.config.github
                and self.config.github.enabled
                and self.config.github.repo_list()
                else "off"
            ),
            (
                "on"
                if self.config.github
                and self.config.github.issues_enabled
                and self.config.github.issue_repo_list()
                else "off"
            ),
        )
        # Reset the activity beacon: a previous run's file must not read as
        # "actively handling" before any work has arrived.
        self._write_activity()
        prune_stale_workspaces(self.config.workspace_dir)
        # A crash mid-session leaves a ledger entry in_flight forever; flip
        # entries with no live tmux session to failed so they're visible (and
        # manually unblockable) instead of masquerading as running.
        reaped = reap_stale_in_flight(_STATE_DIR, is_alive=_tmux_session_alive)
        if reaped:
            _logger.warning(
                "Startup reaper flipped %d stale in_flight stor%s to failed: %s",
                len(reaped),
                "y" if len(reaped) == 1 else "ies",
                ", ".join(str(s) for s in reaped),
            )
        background_tasks: list[asyncio.Task] = []
        if self.config.tickets_enabled:
            await self._requeue_pending_stories()
            for scanner in self._scanners:
                try:
                    enqueued = await scanner.scan()
                    _logger.info(
                        "Backfill scan complete for source '%s': %d tickets enqueued.",
                        scanner._source.id or scanner._source.provider,
                        enqueued,
                    )
                except Exception as e:
                    _logger.exception(
                        "Backfill scan failed for source '%s': %s; continuing",
                        scanner._source.id or scanner._source.provider,
                        e,
                    )
            background_tasks.extend(
                asyncio.create_task(self._poll_loop(scanner))
                for scanner in self._scanners
            )
        else:
            _logger.info(
                "Ticket ingestion is switched off — not scanning or polling "
                "ticketing sources (PR review runs independently)."
            )
        for cache in self.config.caches:
            if not (cache.refresh_enabled and cache.refresh_command):
                continue
            refresher = CacheRefresher(self.config, cache)
            background_tasks.append(asyncio.create_task(refresher.run_forever()))
            _logger.info(
                "Cache refresher enabled: cache=%s branch=%s interval=%ds",
                cache.name,
                cache.refresh_branch,
                cache.refresh_interval_seconds,
            )
        if (
            self._pr_monitor
            and self.config.github
            and self.config.github.enabled
            and self.config.github.repo_list()
        ):
            pr_task = asyncio.create_task(self._pr_loop())
            background_tasks.append(pr_task)
            _logger.info(
                "PR monitor enabled: polling %s every %d seconds (min age %d min).",
                ", ".join(self.config.github.repo_list()),
                self.config.github.poll_interval_seconds,
                self.config.github.min_age_minutes,
            )
        if (
            self._issue_monitor
            and self.config.github
            and self.config.github.issues_enabled
            and self.config.github.issue_repo_list()
        ):
            issue_task = asyncio.create_task(self._issue_loop())
            background_tasks.append(issue_task)
            _logger.info(
                "Issue monitor enabled: polling %s every %d seconds (min age %d min).",
                ", ".join(self.config.github.issue_repo_list()),
                self.config.github.issue_poll_interval_seconds,
                self.config.github.issue_min_age_minutes,
            )
        if self.config.tickets_enabled:
            _logger.info(
                "Polling Shortcut every %d seconds. Entering main processing loop.",
                self.config.poll_interval_seconds,
            )
        try:
            while True:
                item = await self._queue.get()
                self._mark_busy("ticket", +1)
                try:
                    await self.process_story(item)
                except Exception as e:
                    item_id = getattr(item, "id", None) or getattr(
                        item, "story_id", None
                    )
                    _logger.exception(
                        "Error processing item story_id=%s: %s", item_id, e
                    )
                finally:
                    self._mark_busy("ticket", -1)
                    self._queue.task_done()
        finally:
            for task in background_tasks:
                task.cancel()
            for task in background_tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _requeue_pending_stories(self) -> None:
        """Re-enqueue tickets that were enqueued but never picked up.

        The backfill scanner writes a ``pending`` marker per enqueued ticket
        before advancing the poll checkpoint; if the process died with a
        non-empty in-memory queue, those tickets would otherwise be lost
        forever (no ledger entry, checkpoint past their updated_at). Each is
        re-fetched from its source provider so the queued ticket is fresh.
        """
        pending = load_pending_stories(_STATE_DIR)
        if not pending:
            return
        processed_ids = load_processed_story_ids(_STATE_DIR)
        scanners_by_key = {s._source_key: s for s in self._scanners}
        for entry in pending:
            slug = entry.get("story_id")
            if slug in processed_ids:
                # Already picked up (or terminal) — the marker is stale.
                remove_pending_story(_STATE_DIR, slug)
                continue
            scanner = scanners_by_key.get(entry.get("source_key"), self._scanners[0])
            try:
                story = await scanner._provider.fetch(str(entry.get("ticket_id")))
            except Exception as e:  # noqa: BLE001
                # Keep the marker: the next startup retries the re-fetch.
                _logger.warning(
                    "Could not re-fetch pending ticket %s (%s); leaving it "
                    "pending for the next startup",
                    slug,
                    e,
                )
                continue
            story.repo_url = scanner._source.repo_url
            # Re-read from disk, like the scanner's own stamp: a ticket pending
            # since a previous run must launch on the CLI configured NOW, not
            # the one configured when that run booted.
            story.agent = source_agent_now(scanner._source_key, scanner._source.agent)
            await self._queue.put(story)
            _logger.info("Re-enqueued pending ticket %s from a prior run.", slug)

    async def _poll_loop(self, scanner: BackfillScanner) -> None:
        # Each source polls on its own cadence.
        interval = (
            scanner._source.poll_interval_seconds or self.config.poll_interval_seconds
        )
        source_name = scanner._source.id or scanner._source.provider
        while True:
            await asyncio.sleep(interval)
            try:
                enqueued = await scanner.scan()
                if enqueued:
                    _logger.info(
                        "Poll scan for '%s' enqueued %d tickets", source_name, enqueued
                    )
            except Exception as e:
                _logger.exception(
                    "Poll scan for '%s' failed: %s; will retry next interval",
                    source_name,
                    e,
                )

    async def _pr_loop(self) -> None:
        assert self._pr_monitor is not None and self.config.github is not None
        interval = self.config.github.poll_interval_seconds
        while True:
            try:
                prs = await self._pr_monitor.scan()
                if prs:
                    self._mark_busy("pr", +1)
                    try:
                        for pr in prs:
                            try:
                                await self._process_pr(pr)
                            except Exception as e:
                                _logger.exception(
                                    "Failed to process PR #%d: %s", pr.number, e
                                )
                    finally:
                        self._mark_busy("pr", -1)
            except Exception as e:
                _logger.exception("PR scan failed: %s; will retry next interval", e)
            await asyncio.sleep(interval)

    async def _issue_loop(self) -> None:
        assert self._issue_monitor is not None and self.config.github is not None
        interval = self.config.github.issue_poll_interval_seconds
        while True:
            try:
                issues = await self._issue_monitor.scan()
                if issues:
                    self._mark_busy("issue", +1)
                    try:
                        for issue in issues:
                            try:
                                await self._process_issue(issue)
                            except Exception as e:
                                _logger.exception(
                                    "Failed to process issue #%d: %s", issue.number, e
                                )
                    finally:
                        self._mark_busy("issue", -1)
            except Exception as e:
                _logger.exception("Issue scan failed: %s; will retry next interval", e)
            await asyncio.sleep(interval)

    async def _process_issue(self, issue: Issue) -> None:
        assert self.config.github is not None
        _logger.info(
            "Processing issue #%d in %s (%s)", issue.number, issue.repo, issue.title
        )
        try:
            comments = await self._issue_monitor.fetch_comments(issue)
        except IssueCommentsFetchError as e:
            # A transient GitHub failure must not start a session with the
            # discussion silently missing — leave the issue unrecorded so the
            # next poll retries the fetch.
            _logger.warning(
                "Issue #%d: comment fetch failed (%s); will retry next poll.",
                issue.number,
                e,
            )
            return
        story = issue_to_ticket(issue, comments)
        # An issue has no ticketing source to inherit an agent from, so stamp
        # issue handling's own choice onto the ticket here — every downstream
        # launch path already reads `story.agent` first. Blank leaves the
        # existing fallback chain untouched.
        # Re-read at launch, so switching the issue-handling provider in Settings
        # applies to the next issue rather than the next pipeline restart.
        story.agent = agent_now(
            lambda c: c.issue_agent(getattr(issue, "repo", "")),
            self.config.issue_agent(getattr(issue, "repo", "")),
        )
        try:
            if self._cs_runner is not None:
                await self._cs_runner.run(story)
            else:
                env = await self._provisioner.provision(story)
                await self._claude_runner.invoke(
                    env=env, story=story, supplemental_context=None
                )
        except Exception as e:
            # Cap retries like _process_pr: after _ISSUE_MAX_ATTEMPTS, record
            # the issue processed-as-failed (manual unblock: delete the entry
            # from state.json's processed_issues).
            attempts = record_issue_attempt(_STATE_DIR, issue.repo, issue.number)
            if attempts >= _ISSUE_MAX_ATTEMPTS:
                _logger.error(
                    "Issue #%d failed %d/%d provisioning attempts (%s); giving up "
                    "and recording it as processed (failed). Delete its "
                    "processed_issues entry in state.json to retry.",
                    issue.number,
                    attempts,
                    _ISSUE_MAX_ATTEMPTS,
                    e,
                )
                record_processed_issue(
                    _STATE_DIR,
                    ProcessedIssue(
                        number=issue.number,
                        processed_at=datetime.now(timezone.utc),
                        repo=issue.repo,
                        status="failed",
                    ),
                )
                clear_issue_attempts(_STATE_DIR, issue.repo, issue.number)
                return
            raise
        clear_issue_attempts(_STATE_DIR, issue.repo, issue.number)
        record_processed_issue(
            _STATE_DIR,
            ProcessedIssue(
                number=issue.number,
                processed_at=datetime.now(timezone.utc),
                repo=issue.repo,
            ),
        )

    async def _process_pr(self, pr) -> None:
        assert self.config.github is not None
        _logger.info("Processing PR #%d (%s)", pr.number, pr.title)
        try:
            comments = await fetch_actionable_comments(pr, self.config.github)
        except PRCommentsFetchError as e:
            # Transient GitHub failure must not read as "no comments" and
            # permanently record the PR as processed — leave it unrecorded so
            # the next poll retries the fetch.
            _logger.warning(
                "PR #%d: comment fetch failed (%s); will retry next poll.",
                pr.number,
                e,
            )
            return
        _logger.info(
            "PR #%d: %d actionable comments after filtering", pr.number, len(comments)
        )
        if not comments:
            _logger.info(
                "PR #%d: skipping provisioning; no actionable comments.", pr.number
            )
            record_processed_pr(
                _STATE_DIR,
                ProcessedPR(
                    number=pr.number,
                    head_sha=pr.head_sha,
                    processed_at=datetime.now(timezone.utc),
                    repo=getattr(pr, "repo", ""),
                ),
            )
            return
        try:
            if self._cs_runner is not None:
                # One consolidated session per PR via MindFlock (all comments
                # addressed in a single window), instead of a tab per comment.
                await self._cs_runner.run_pr(pr, comments)
            else:
                workspace = await self._pr_provisioner.provision(pr)
                # Refresh the CLI first: the runner was built with the provider
                # configured at process start, which may be several Settings
                # changes ago.
                self._pr_runner.agent = agent_now(
                    lambda c: c.pr_agent(getattr(pr, "repo", "")),
                    self.config.pr_agent(getattr(pr, "repo", "")),
                )
                await self._pr_runner.launch(pr, workspace, comments)
        except Exception as e:
            # Cap retries: without a record, a PR whose provisioning keeps
            # failing would be re-cloned on every poll forever. After
            # _PR_MAX_ATTEMPTS, record it processed-as-failed (manual unblock:
            # delete the entry from state.json's processed_prs).
            attempts = record_pr_attempt(_STATE_DIR, getattr(pr, "repo", ""), pr.number)
            if attempts >= _PR_MAX_ATTEMPTS:
                _logger.error(
                    "PR #%d failed %d/%d provisioning attempts (%s); giving up "
                    "and recording it as processed (failed). Delete its "
                    "processed_prs entry in state.json to retry.",
                    pr.number,
                    attempts,
                    _PR_MAX_ATTEMPTS,
                    e,
                )
                record_processed_pr(
                    _STATE_DIR,
                    ProcessedPR(
                        number=pr.number,
                        head_sha=pr.head_sha,
                        processed_at=datetime.now(timezone.utc),
                        repo=getattr(pr, "repo", ""),
                        status="failed",
                    ),
                )
                clear_pr_attempts(_STATE_DIR, getattr(pr, "repo", ""), pr.number)
                return
            raise
        clear_pr_attempts(_STATE_DIR, getattr(pr, "repo", ""), pr.number)
        record_processed_pr(
            _STATE_DIR,
            ProcessedPR(
                number=pr.number,
                head_sha=pr.head_sha,
                processed_at=datetime.now(timezone.utc),
                repo=getattr(pr, "repo", ""),
            ),
        )

    async def process_story(self, item: Ticket | WebhookEvent) -> None:
        if isinstance(item, WebhookEvent):
            story = await self._fetch_story(item.story_id)
        else:
            story = item

        # The story left the in-memory queue: its crash-recovery ``pending``
        # marker (written at enqueue time, see BackfillScanner.scan) has done
        # its job — from here the processed_stories ledger takes over.
        remove_pending_story(_STATE_DIR, story.slug)

        # Idempotency: the ledger used to be written only AFTER a (long)
        # session finished, so a poll scan running mid-session re-passed the
        # processed/branch guards and enqueued a duplicate. Two guards close
        # that TOCTOU window:
        #   1. drop anything already recorded (completed / skipped / failed /
        #      a concurrent in_flight) the moment it is dequeued — catches
        #      duplicates that made it into the queue, and webhook re-fires;
        #   2. record an ``in_flight`` marker before any long-running step
        #      (clarification, provisioning, the session itself) so scans that
        #      run mid-session see the story as taken.
        # To deliberately re-run a story, delete its entry from state.json's
        # processed_stories (same manual unblock as processed_prs).
        processed_ids = load_processed_story_ids(_STATE_DIR)
        if story.slug in processed_ids or story.id in processed_ids:
            _logger.info(
                "Skipping %s: already in processed_stories (duplicate enqueue, "
                "in-flight session, or webhook re-fire).",
                story.slug,
            )
            return

        if not self._assignee_filter.is_assigned(story):
            record_processed_story(
                _STATE_DIR,
                ProcessingRecord(
                    story_id=story.slug,
                    branch=story.slug,
                    status="skipped",
                    processed_at=datetime.now(timezone.utc),
                    failure_reason="not assigned to target member",
                ),
            )
            return

        # In-flight marker (guard 2): from here on, concurrent scans and
        # duplicate dequeues treat this story as processed.
        record_processed_story(
            _STATE_DIR,
            ProcessingRecord(
                story_id=story.slug,
                branch=story.slug,
                status="in_flight",
                processed_at=datetime.now(timezone.utc),
            ),
        )

        try:
            validation = self._validator.validate(story)
            supplemental_context: str | None = None

            if not validation.is_valid:
                if self._cs_runner is not None:
                    # Engine mode: keep it to ONE MindFlock window. Fold the
                    # clarification request into that session's prompt as supplemental
                    # context (the agent asks the developer for the missing details in
                    # the same window) instead of spawning a separate standalone Cursor +
                    # terminal clarification session.
                    supplemental_context = (
                        self._clarification_handler.clarification_context(
                            story, validation
                        )
                    )
                else:
                    clarification = (
                        await self._clarification_handler.request_clarification(
                            story, validation
                        )
                    )
                    if clarification.action == "skip":
                        update_processed_story(
                            _STATE_DIR,
                            story.slug,
                            status="skipped",
                            failure_reason="developer chose to skip during clarification",
                        )
                        return
                    supplemental_context = clarification.supplemental_context

            if self._cs_runner is not None:
                branch = await self._cs_runner.run(
                    story, supplemental_context=supplemental_context
                )
            else:
                env = await self._provisioner.provision(story)
                await self._claude_runner.invoke(
                    env=env, story=story, supplemental_context=supplemental_context
                )
                branch = env.branch_name
        except Exception as e:
            # Flip the in-flight marker to a terminal ``failed`` so the story
            # is not auto-retried on the next scan (a half-provisioned
            # workspace + retry loop is worse than a manual unblock: delete
            # the ledger entry to re-run it).
            update_processed_story(
                _STATE_DIR,
                story.slug,
                status="failed",
                failure_reason=str(e),
            )
            raise

        update_processed_story(
            _STATE_DIR,
            story.slug,
            status="completed",
            branch=branch,
        )

    async def _fetch_story(self, story_id: int | str) -> Ticket:
        """Full ticket detail for a webhook event, via the active provider."""
        return await self._provider.fetch(str(story_id))
