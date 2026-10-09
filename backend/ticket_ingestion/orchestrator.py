"""Pipeline orchestrator: wires components together and drains the queue."""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.ticket_ingestion.backfill import BackfillScanner
from backend.ticket_ingestion.providers import get_provider
from backend.ticket_ingestion.clarification import InteractiveClarificationHandler
from backend.ticket_ingestion.claude_runner import ClaudeCodeRunner
from backend.ticket_ingestion.session_runner import SessionRunner, engine_bridge_error
from backend.ticket_ingestion.config import (
    PipelineConfig,
    agent_now,
    max_sessions_now,
    pipeline_in_group,
    source_agent_now,
    source_effort_now,
)
from backend.ticket_ingestion.filter import AssigneeFilter
from backend.ticket_ingestion.providers.base import (
    TicketNotFound,
    ingest_filter_miss,
    ingests_any_assignee,
)
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
from backend.ticket_ingestion.provisioner import (
    EnvironmentProvisioner,
    _open_ide_on_ticket,
)
from backend.ticket_ingestion.state import (
    automation_handover,
    clear_issue_attempts,
    clear_pr_attempts,
    ledger_started,
    load_pending_stories,
    load_processed_story_ids,
    load_processed_story_statuses,
    mark_automation_here,
    mark_ledger_started,
    reap_stale_in_flight,
    record_issue_attempt,
    record_pr_attempt,
    record_processed_issue,
    record_processed_pr,
    record_processed_story,
    remove_in_flight_story,
    remove_pending_story,
    seed_processed_issues,
    seed_processed_prs,
    update_processed_story,
)
from backend.ticket_ingestion.cache_refresher import CacheRefresher
from backend.ticket_ingestion.validator import TicketValidator
from backend.ticket_ingestion.workspace_cleanup import prune_stale_workspaces

_logger = logging.getLogger(__name__)

_STATE_DIR = Path(".")


def _fleet_holder(slug: str, since: float) -> str:
    """Why another of the user's devices holds ``slug`` (``""`` when none),
    asked of this machine's own MindFlock server, which knows the paired
    devices (``GET /api/tickets/fleet-holder``). Fails open: no server, an
    old one, or an error means "nobody" — the local guards still apply."""
    port = (os.environ.get("MINDFLOCK_SERVER_PORT") or "").strip()
    if not port.isdigit():
        return ""
    from urllib.parse import urlencode

    from backend import client

    try:
        body = client.get(
            client.base_url("127.0.0.1", int(port)),
            "/api/tickets/fleet-holder?" + urlencode({"slug": slug, "since": since}),
            timeout=15.0,
        )
    except Exception:  # noqa: BLE001 — fail open
        return ""
    if isinstance(body, dict) and body.get("holder"):
        return str(body.get("reason") or "held by another device")
    return ""


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
# How often a ticket held back by the concurrent-session cap re-checks for a
# free slot (a session ending is only visible by polling tmux).
_SLOT_POLL_SECONDS = 10.0


def _live_tmux_sessions() -> set[str] | None:
    """Every tmux session name on the default server, or None when tmux can't
    be listed (missing / timed out). No server running means no sessions."""
    try:
        proc = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def live_ticket_sessions(state_dir: Path | str = _STATE_DIR) -> int | None:
    """How many ingested ticket sessions are alive right now.

    A ticket's session is alive while its tmux session is: ``mindflock_<slug>``
    in engine mode (dots become ``_``, as tmux does), ``<slug>`` standalone.
    Only ledger entries a session was launched for count — skipped / failed
    tickets never got one. Force-started tickets (Intake → Start now) write the
    same ledger entry, so they count against the cap too. None when tmux can't
    be probed.
    """
    names = _live_tmux_sessions()
    if names is None:
        return None
    count = 0
    for slug, status in load_processed_story_statuses(state_dir).items():
        if status not in ("in_flight", "completed"):
            continue
        slug = str(slug)
        if f"mindflock_{slug.replace('.', '_')}" in names or slug in names:
            count += 1
    return count


def _reservation_alive(holder: str) -> bool | None:
    """Whether the holder of a ledger RESERVATION still exists: ``run:<id>``
    is a team run that has not finished (it hands its queued tickets back
    itself). None when it cannot be told."""
    if not holder.startswith("run:"):
        return None
    try:
        from backend.web.core import team_runs

        run = team_runs.load(holder[len("run:") :])
    except Exception:  # noqa: BLE001
        return None
    return run is not None and run["state"] not in team_runs.RUN_FINISHED


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
        #
        # The net is flock-wide and the work queue doesn't record which source a
        # story came from, so one source set to ingest any assignee disarms it
        # entirely: those tickets are meant to belong to other people, and an
        # id-based net has no way to tell them from a mis-scoped fetch on a
        # neighbouring source. Each provider's own server-side scoping stays the
        # real filter either way.
        member_ids = (
            []
            if any(ingests_any_assignee(s) for s in sources)
            else [s.member_id for s in sources if s.member_id]
        )
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
        # Set while a dequeued ticket waits on the concurrent-session cap:
        # {"live": n, "max": m}. Mirrored to the beacon for the Intake UI.
        self._held: dict[str, int] | None = None
        # PR review / issue handling just moved here from another of the
        # user's devices: both loops wait for the ledgers to be seeded first.
        self._handover_lock = asyncio.Lock()
        self._handover_done = False

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
                        "held_for_slot": self._held,
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
        reaped = reap_stale_in_flight(
            _STATE_DIR,
            is_alive=_tmux_session_alive,
            reservation_alive=_reservation_alive,
        )
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
                # Wait AFTER dequeuing: a slot freed while the queue was empty
                # could be taken by a hand-started session before this ticket
                # arrives. The item keeps its crash-recovery pending marker
                # until process_story runs, so a scan can't re-enqueue it.
                await self._wait_for_slot()
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

    def _scanner_for(self, source_key: str | None) -> BackfillScanner:
        """The scanner (source + provider) a queued ticket came from; the
        primary one for an unknown or unstamped key."""
        for scanner in self._scanners:
            if scanner._source_key == source_key:
                return scanner
        return self._scanners[0]

    async def _requeue_pending_stories(self) -> None:
        """Re-enqueue tickets that were enqueued but never picked up.

        The backfill scanner writes a ``pending`` marker per enqueued ticket
        before advancing the poll checkpoint; if the process died with a
        non-empty in-memory queue, those tickets would otherwise be lost
        forever (no ledger entry, checkpoint past their updated_at). Each is
        re-fetched from its source provider so the queued ticket is fresh.

        …and re-checked against its source's ingest filters, because a marker
        can outlive the reason it was written by days: a burst of new tickets
        queued behind the session cap, triaged back out of the ingest state,
        still came back on every restart and launched — straight into the
        start state, undoing the triage. A ticket that no longer passes, or no
        longer exists, loses its marker here instead.
        """
        pending = load_pending_stories(_STATE_DIR)
        if not pending:
            return
        processed_ids = load_processed_story_ids(_STATE_DIR)
        for entry in pending:
            slug = entry.get("story_id")
            if slug in processed_ids:
                # Already picked up (or terminal) — the marker is stale.
                remove_pending_story(_STATE_DIR, slug)
                continue
            scanner = self._scanner_for(entry.get("source_key"))
            try:
                story = await scanner._provider.fetch(str(entry.get("ticket_id")))
            except TicketNotFound as e:
                remove_pending_story(_STATE_DIR, slug)
                _logger.info(
                    "Dropped pending ticket %s: it no longer exists (%s).", slug, e
                )
                continue
            except Exception as e:  # noqa: BLE001
                # Keep the marker: the next startup retries the re-fetch.
                _logger.warning(
                    "Could not re-fetch pending ticket %s (%s); leaving it "
                    "pending for the next startup",
                    slug,
                    e,
                )
                continue
            miss = ingest_filter_miss(scanner._source, story)
            if miss:
                remove_pending_story(_STATE_DIR, slug)
                _logger.info("Dropped pending ticket %s: %s.", slug, miss)
                continue
            story.repo_url = scanner._source.repo_url
            # Re-read from disk, like the scanner's own stamp: a ticket pending
            # since a previous run must launch on the CLI configured NOW, not
            # the one configured when that run booted.
            story.agent = source_agent_now(scanner._source_key, scanner._source.agent)
            story.effort = source_effort_now(
                scanner._source_key, scanner._source.effort
            )
            story.source_key = scanner._source_key
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

    async def _wait_for_slot(self) -> None:
        """Block until fewer ticket sessions are alive than the configured cap.

        The cap is re-read every check, so raising it (or setting 0 = no limit)
        in Intake releases the queue without a restart. An unprobeable tmux
        doesn't hold the queue: the cap is a safety valve, not a gate that may
        wedge ingestion.
        """
        logged = False
        try:
            while True:
                cap = max_sessions_now(
                    self.config.engine.max_sessions if self.config.engine else 0
                )
                if cap <= 0:
                    return
                live = await asyncio.to_thread(live_ticket_sessions, _STATE_DIR)
                if live is None or live < cap:
                    return
                held = {"live": live, "max": cap}
                if held != self._held:
                    self._held = held
                    self._write_activity()
                if not logged:
                    _logger.info(
                        "Holding the next ticket: %d of %d ticket sessions are "
                        "running (Intake → Auto-start → max sessions). It will "
                        "start when one ends.",
                        live,
                        cap,
                    )
                    logged = True
                await asyncio.sleep(_SLOT_POLL_SECONDS)
        finally:
            if self._held is not None:
                self._held = None
                self._write_activity()

    async def _ensure_handover(self) -> None:
        """Before the first PR / issue scan, seed the processed-PR and
        processed-issue ledgers with what is open right now — another of the
        user's devices already handled those, and this device's ledgers
        don't know it — when either:

        * PR review and issue handling just moved here from another device
          (the server records that — :func:`state.note_automation`); or
        * this device is grouped with another live device
          (:func:`config.pipeline_in_group`) and that loop never ran here
          (:func:`state.ledger_started`): it starts here for the first time,
          whenever the choice of device was made — e.g. it was the chosen
          device before the group's repos reached it by settings sync.

        Raises when GitHub can't be listed (the loop retries next interval,
        scanning nothing meanwhile)."""
        if self._handover_done:
            return
        async with self._handover_lock:
            if self._handover_done:
                return
            prev = automation_handover(_STATE_DIR)
            grouped = pipeline_in_group()
            gh = self.config.github
            pr_on = self._pr_monitor is not None and gh and gh.enabled
            issue_on = self._issue_monitor is not None and gh and gh.issues_enabled
            seed_prs = pr_on and (
                prev is not None or (grouped and not ledger_started(_STATE_DIR, "prs"))
            )
            seed_issues = issue_on and (
                prev is not None
                or (grouped and not ledger_started(_STATE_DIR, "issues"))
            )
            source = prev or "another device"
            # Only what the other device could already have taken: an item
            # still inside its grace period wasn't eligible there yet, so
            # seeding it would mean nobody ever handles it.
            now = datetime.now(timezone.utc)
            if seed_prs:
                prs = []
                for repo in gh.repo_list():
                    prs.extend(
                        p
                        for p in await self._pr_monitor._list_prs(repo)
                        if p.created_at
                        <= now - timedelta(minutes=gh.min_age_for(p.repo))
                    )
                n = seed_processed_prs(
                    _STATE_DIR, [(p.repo, p.number, p.head_sha) for p in prs]
                )
                _logger.info(
                    "PR review moved here — %d open PRs already handled on %s "
                    "are skipped",
                    n,
                    source,
                )
            if seed_issues:
                issues = []
                for repo in gh.issue_repo_list():
                    issues.extend(
                        i
                        for i in await self._issue_monitor._list_issues(repo)
                        if i.created_at
                        <= now - timedelta(minutes=gh.issue_min_age_for(i.repo))
                    )
                n = seed_processed_issues(
                    _STATE_DIR, [(i.repo, i.number) for i in issues]
                )
                _logger.info(
                    "Issue handling moved here — %d open issues already handled "
                    "on %s are skipped",
                    n,
                    source,
                )
            if pr_on:
                mark_ledger_started(_STATE_DIR, "prs")
            if issue_on:
                mark_ledger_started(_STATE_DIR, "issues")
            if prev is not None:
                mark_automation_here(_STATE_DIR)
            self._handover_done = True

    async def _pr_loop(self) -> None:
        assert self._pr_monitor is not None and self.config.github is not None
        interval = self.config.github.poll_interval_seconds
        while True:
            try:
                await self._ensure_handover()
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
                await self._ensure_handover()
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
                workspace = await self._pr_provisioner.provision(
                    pr, launch_cursor=_open_ide_on_ticket()
                )
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

        # A queued ticket is a snapshot from its scan, and it may have waited
        # hours behind the session cap. Nothing is recorded when it's dropped:
        # moved back into the ingest state, it's a fresh poll match again.
        if isinstance(item, Ticket):
            miss = await self._ingest_filter_miss_now(story)
            if miss:
                _logger.info(
                    "Not launching %s: %s since it was queued.", story.slug, miss
                )
                return

        # In-flight marker (guard 2): from here on, concurrent scans and
        # duplicate dequeues treat this story as processed.
        marked_at = datetime.now(timezone.utc)
        record_processed_story(
            _STATE_DIR,
            ProcessingRecord(
                story_id=story.slug,
                branch=story.slug,
                status="in_flight",
                processed_at=marked_at,
            ),
        )

        # Guard 3: another of the user's devices may hold it (fleet_claims).
        # Asked AFTER our own marker is down, with its time, so of two devices
        # racing for one ticket exactly one backs off. Nothing is recorded on a
        # back-off: the ticket is the other device's, not done here.
        elsewhere = await asyncio.to_thread(
            _fleet_holder, story.slug, marked_at.timestamp()
        )
        if elsewhere:
            remove_in_flight_story(_STATE_DIR, story.slug)
            _logger.info("Not launching %s: %s.", story.slug, elsewhere)
            return

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

    async def _ingest_filter_miss_now(self, story: Ticket) -> str:
        """Why a queued ticket should not launch after all, or ``""``.

        Re-reads the ticket and re-applies its source's ingest filters
        (:func:`ingest_filter_miss`). A ticket deleted while it waited fails
        too. A read that fails for any other reason launches the ticket as
        queued: the scan already vetted it, and a flaky API must not silently
        eat real work.
        """
        scanner = self._scanner_for(story.source_key)
        try:
            fresh = await scanner._provider.fetch(str(story.id))
        except TicketNotFound:
            return "it no longer exists"
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "Could not re-check %s before launch (%s); launching it as queued.",
                story.slug,
                e,
            )
            return ""
        return ingest_filter_miss(scanner._source, fresh)

    async def _fetch_story(self, story_id: int | str) -> Ticket:
        """Full ticket detail for a webhook event, via the active provider."""
        story = await self._provider.fetch(str(story_id))
        # The webhook path runs on the PRIMARY source, so that is the source
        # whose settings this ticket launches under (start_state).
        primary = self.config.ticketing
        if primary is not None:
            story.source_key = primary.id or primary.provider
        return story
