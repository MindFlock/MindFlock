"""Force-start ticket sessions from the web UI (Intake → Tickets).

The automated pipeline (``backend.ticket_ingestion``) only picks up
tickets that are (a) assigned to the configured user and updated since the
per-source poll checkpoint, (b) absent from state.json's
``processed_stories`` ledger and (c) without an existing
``feature/<slug>/…`` branch on the remote — so a ticket can silently never
get ingested (most commonly: it was recorded as failed/skipped once, or its
branch was pushed by hand). This module backs the Intake → Tickets
"Assigned tickets" panel: it lists recently-updated tickets assigned to you
on every configured source annotated with *why* auto ingestion is or isn't
taking each one, and force-starts a session for any of them, bypassing
those filters.

The forced path reuses the pipeline's own pieces (provider adapters, prompt
builder, branch naming, the processed-stories ledger) but runs inside the
web server process, so it works even while ingestion is stopped.
``server.py`` owns the engine side (registering the session in the live
grid) — mirroring :mod:`backend.web.core.pr_review` exactly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)

#: Bucket label for tickets whose provider doesn't report a workflow state.
NO_STATE_BUCKET = "No state"


def _resolve_repo_root() -> Path:
    """Where the pipeline keeps state.json / workspaces/ — same resolution
    order as the ingestion addon and :mod:`backend.web.core.pr_review`:
    ``MINDFLOCK_REPO_ROOT`` env → nearest ancestor with ``config.toml`` → cwd.
    """
    env = (os.environ.get("MINDFLOCK_REPO_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "config.toml").is_file():
            return parent
    return Path.cwd()


_REPO_ROOT = _resolve_repo_root()


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import).

    :func:`launch` registers the session in the server's live engine, so it
    calls back through the server namespace for everything it needs from
    there — which also keeps ``monkeypatch.setattr(server, "_foo", …)``
    working wherever ``_foo`` is used."""
    from backend.web import server

    return server


def _load_config():
    """The pipeline's layered config (env → settings.json → config.toml),
    with the relative ``workspace_dir`` default re-anchored at the repo root
    (this server's cwd is not guaranteed to match the pipeline's)."""
    from backend.ticket_ingestion.config import load_config

    cfg = load_config()
    if not Path(cfg.workspace_dir).is_absolute():
        cfg.workspace_dir = _REPO_ROOT / cfg.workspace_dir
    return cfg


def session_title(story) -> str:
    """Engine session title for a ticket — the slug, matching
    ``SessionRunner`` so the panel's has-session check and the pipeline's own
    sessions collide correctly (one session per ticket either way)."""
    return story.slug


def workspace_mode() -> str:
    """The engine workspace strategy tickets provision with (worktree/clone),
    matching ``SessionRunner``'s choice for pipeline-launched sessions."""
    try:
        cfg = _load_config()
        return cfg.engine.mode if cfg.engine and cfg.engine.mode else "worktree"
    except Exception:  # noqa: BLE001
        return "worktree"


def agent_for(story) -> str:
    """The agent CLI a forced ticket session should run, or ``""``.

    Same chain ``SessionRunner`` applies to a pipeline-launched ticket (the
    source's ``agent``, then ``[mindflock].agent``), so a ticket started by hand
    from the Assigned-tickets panel runs the CLI its source is configured for
    rather than the app's global default. ``""`` = use that global default.
    """
    if getattr(story, "agent", ""):
        return story.agent
    try:
        return _load_config().agent_for(getattr(story, "provider", ""))
    except Exception:  # noqa: BLE001
        return ""


def effort_for(story) -> str:
    """The thinking-effort rung a forced ticket session should run at, or ``""``.

    The effort twin of :func:`agent_for`, and it exists for the same reason: a
    ticket started by hand from the panel should run the way its source is
    configured to run, not at whatever the CLI happens to default to. The
    ticket's own stamp wins (the orchestrator copies it from the source that
    produced the ticket), then the source's setting as it is on disk now.

    ``""`` = let the CLI decide. A per-start override on the row still beats this
    — the caller applies that first.
    """
    if getattr(story, "effort", ""):
        return story.effort
    try:
        return _load_config().effort_for(getattr(story, "provider", ""))
    except Exception:  # noqa: BLE001
        return ""


def effort_for(story) -> str:
    """The thinking-effort rung a forced ticket session should run at, or ``""``.

    The effort twin of :func:`agent_for`, and the same chain
    ``SessionRunner._effort_for`` applies to a pipeline-launched ticket — the
    ticket's own stamp, then the source's configured rung — so starting a ticket
    by hand from the panel thinks as hard as letting the pipeline pick it up
    would. Without this, the two paths disagreed about the one setting whose
    whole point is "every ticket from this queue, not just the ones I remember to
    set".

    ``""`` = whatever the CLI does on its own. There is deliberately no
    installation-wide fallback; see ``PipelineConfig.effort_for``.
    """
    if getattr(story, "effort", ""):
        return story.effort
    try:
        return _load_config().effort_for(getattr(story, "provider", ""))
    except Exception:  # noqa: BLE001
        return ""


def branch_for(story) -> str:
    """The ``feature/<slug>/<name-slug>`` branch the pipeline would push for
    ``story`` — the pipeline's own naming, so a forced start and an auto ingest
    of the same ticket land on the same branch."""
    from backend.ticket_ingestion.provisioner import _branch_name_for

    return _branch_name_for(story)


def _failed_label(reason: str) -> str:
    """The chip text for a ``failed`` ledger entry.

    This used to read "failed earlier — delete its state.json ledger entry to
    retry", which was wrong twice over: force-start never consults the ledger
    (only a live session blocks it), and the common cause is a leftover worktree
    still holding the branch — so deleting the ledger entry drops the record of
    the failure and the retry fails identically. Showing what actually went
    wrong is the difference between an actionable chip and a wild goose chase.

    Returned in full, only whitespace-normalized: the useful half of these
    reasons ("… is already checked out at <path>") is at the END, so clipping to
    a chip-sized budget here would cut off the part worth reading. The chip wraps
    in CSS and carries the whole string in its tooltip.
    """
    if not reason:
        return "failed earlier (no reason recorded) — Run ticket to retry"
    return "failed earlier: " + " ".join(reason.split())


def skip_reasons(
    story,
    ledger: dict,
    pending_ids: set,
    branches: set,
    member_ids,
    ingest_state: list | None = None,
    failures: dict | None = None,
    ingest_labels: list | None = None,
) -> list[str]:
    """Why auto ingestion would skip ``story`` right now (empty = eligible).

    Mirrors the exact filters in ``BackfillScanner.scan`` and
    ``PipelineOrchestrator.process_story`` so the UI explains the pipeline's
    behavior instead of guessing at it. ``ingest_state`` is the source's
    configured workflow-state filter (one or more states, resolved to their
    display names) — the provider applies it server-side in
    ``search_assigned``, so a ticket in any other bucket is never
    auto-ingested; the unfiltered panel listing has to re-state that here.
    Only checked when the ticket's own state is known (providers that don't
    annotate it already filtered server-side). ``ingest_labels`` is the same
    idea for the source's label filter: a ticket carrying none of them is
    never auto-ingested, however eligible it is otherwise.
    """
    from backend.ticket_ingestion.filter import AssigneeFilter
    from backend.ticket_ingestion.providers.base import has_ingest_label

    reasons: list[str] = []
    if ingest_state and story.state and story.state not in ingest_state:
        reasons.append("not in an ingest state — won't auto-ingest")
    if ingest_labels and not has_ingest_label(story.labels, ingest_labels):
        reasons.append(
            "missing an ingest label (" + ", ".join(ingest_labels) + ")"
            " — won't auto-ingest"
        )
    status = ledger.get(story.slug) or ledger.get(str(story.id))
    if status:
        if status == "failed":
            f = failures or {}
            label = _failed_label(f.get(story.slug) or f.get(str(story.id)) or "")
        else:
            label = {
                "completed": "already ingested (recorded completed in the ledger)",
                "in_flight": "a session for it is in flight",
                "skipped": "skipped earlier (recorded in the ledger)",
            }.get(status, f"already in the processed ledger ({status})")
        reasons.append(label)
    if story.slug in pending_ids:
        reasons.append("queued for ingestion (pending)")
    prefix = f"feature/{story.slug}/"
    if any(b.startswith(prefix) for b in branches):
        reasons.append("a feature branch for it already exists on the remote")
    if not AssigneeFilter(member_ids).is_assigned(story):
        reasons.append("not assigned to the configured member id")
    return reasons


async def list_assigned_tickets() -> dict:
    """EVERY ticket assigned to you on every configured source, annotated for
    the UI and tagged with its workflow-state bucket. ``buckets`` carries the
    bucket names in workflow order (so the panel can render Backlog → … →
    Done rather than alphabetically); tickets whose provider doesn't report a
    state land in the trailing ``No state`` bucket. Sources that fail (bad
    token, network) are reported per-source instead of failing the panel."""
    from backend.ticket_ingestion.backfill import _get_existing_branches
    from backend.ticket_ingestion.providers import get_provider
    from backend.ticket_ingestion.state import (
        load_pending_stories,
        load_processed_story_failures,
        load_processed_story_statuses,
    )

    cfg = _load_config()
    sources = cfg.ticketing_sources or []
    # One resolution for the whole listing: the strategy is app-wide, and the
    # per-row copy is what lets the reopen probe know where a previous run's
    # workspace would be (a worktree off the base clone, or its own clone).
    strategy = workspace_mode()
    ledger = load_processed_story_statuses(_REPO_ROOT)
    failures = load_processed_story_failures(_REPO_ROOT)
    pending_ids = {e.get("story_id") for e in load_pending_stories(_REPO_ROOT)}

    tickets: list[dict] = []
    errors: list[dict] = []
    listed_sources: list[str] = []
    # source key -> human label, for EVERY configured source including ones with
    # no tickets and ones that failed. The panel groups by source, so a source
    # that returned nothing still needs a heading to say so under — deriving
    # labels from the ticket rows alone would make those sources vanish.
    source_labels: dict = {}
    # bucket name -> {"group": <workflow/board>, "label": <unqualified state>}.
    # Lets the panel render source -> workflow -> state instead of stapling the
    # workflow name onto every state heading.
    bucket_meta: dict = {}
    bucket_order: list[str] = []
    done_buckets: set = set()
    ingest_states: dict = {}  # source key -> configured ingest bucket name
    branch_cache: dict[str, set] = {}
    for src in sources:
        source_key = src.id or src.provider
        # The user's own label wins; the provider's is the fallback, and it is
        # only reachable once the adapter constructs, so seed the key first.
        source_labels[source_key] = (
            getattr(src, "label", "") or ""
        ).strip() or source_key
        try:
            provider = get_provider(src)
            stories = await provider.search_assigned_all()
        except Exception as err:  # noqa: BLE001 — token / network / config
            errors.append({"source": source_key, "error": str(err)})
            continue
        listed_sources.append(source_key)
        source_labels[source_key] = (
            (getattr(src, "label", "") or "").strip()
            or getattr(provider, "label", "")
            or source_key
        )
        # Bucket order = the provider's workflow order (best-effort; a failed
        # states call just means this source's buckets append as encountered).
        # done-type buckets (Completed, Won't do, …) are flagged so the UI can
        # park them behind the Add menu by default — they typically dwarf the
        # actionable ones.
        states_by_id: dict = {}
        try:
            for st in await provider.list_states():
                name = st.get("name") or str(st.get("id"))
                states_by_id[str(st.get("id"))] = name
                if name and name not in bucket_order:
                    bucket_order.append(name)
                if name and str(st.get("type") or "") == "done":
                    done_buckets.add(name)
                # The workflow/board this state sits in, and its unqualified
                # name — so the panel can nest rather than repeat the qualifier
                # on every heading. Keyed by the bucket name, like every other
                # bucket-indexed map here; two sources sharing a bucket name
                # already share the bucket, so the last writer winning is the
                # same answer the rest of this payload gives.
                if name:
                    bucket_meta[name] = {
                        "group": str(st.get("group") or ""),
                        "label": str(st.get("label") or "") or name,
                    }
        except Exception:  # noqa: BLE001
            pass
        # The source's configured ingest filter (one or more states), as
        # bucket names — tickets in any other bucket must not read as
        # "queued for auto ingestion".
        from backend.ticket_ingestion.providers.base import (
            ingest_label_list,
            ingests_any_assignee,
            workflow_state_list,
        )

        state_filter = workflow_state_list(src)
        if not state_filter and getattr(src, "workflow_state_id", None) is not None:
            state_filter = [str(src.workflow_state_id)]
        ingest_state = [states_by_id[s] for s in state_filter if s in states_by_id]
        if ingest_state:
            ingest_states[source_key] = ingest_state
        repo = (src.repo_url or cfg.repo_url or "").strip()
        if repo not in branch_cache:
            try:
                branch_cache[repo] = (
                    await _get_existing_branches(repo) if repo else set()
                )
            except Exception:  # noqa: BLE001 — listing still works without it
                branch_cache[repo] = set()
        # An any-assignee source's tickets belong to other people by design, so
        # "not assigned to you" is not a reason to skip one there.
        any_assignee = ingests_any_assignee(src)
        member_ids = [src.member_id] if src.member_id and not any_assignee else []
        # Whether this source's adapter can fold one of its tickets into another
        # and delete the loser. Stamped per row rather than looked up in the UI
        # because the answer is the adapter's (``TicketProvider.can_merge``),
        # and a client-side list of "providers that support merging" is a second
        # copy of that fact waiting to disagree with the first.
        merge_ready = bool(getattr(provider, "can_merge", False))
        for story in stories:
            story.repo_url = src.repo_url
            story.agent = getattr(src, "agent", "")
            reasons = skip_reasons(
                story,
                ledger,
                pending_ids,
                branch_cache[repo],
                member_ids,
                ingest_state=ingest_state,
                failures=failures,
                ingest_labels=ingest_label_list(src),
            )
            bucket = story.state or NO_STATE_BUCKET
            if bucket not in bucket_order:
                bucket_order.append(bucket)
            tickets.append(
                {
                    "source": source_key,
                    "source_label": source_labels[source_key],
                    "id": str(story.id),
                    "slug": story.slug,
                    "name": story.name,
                    "url": story.app_url,
                    "created_at": story.created_at.isoformat(),
                    "session": session_title(story),
                    # What a run of this ticket owns on disk: the branch it
                    # takes, the repo it provisions from and how. The panel
                    # never shows them; they are what
                    # :mod:`backend.web.core.reopen` needs to find a workspace a
                    # previous run left behind (see the annotation in server.py).
                    "branch": branch_for(story),
                    "repo_url": repo,
                    "strategy": strategy,
                    "bucket": bucket,
                    "merge_ready": merge_ready,
                    "eligible": not reasons,
                    "reasons": reasons,
                    # Whose ticket this is. A source scoped to "anyone" lists
                    # other people's work, and the panel has to be able to say
                    # so — and to filter down to your own again. Anywhere else
                    # the provider already searched by assignee, so every row is
                    # yours whether or not a member id was ever filled in.
                    "mine": (not any_assignee)
                    or bool(src.member_id and src.member_id in story.owner_ids),
                    "assignee": ", ".join(story.owner_names),
                }
            )
    # Only buckets that actually hold tickets; No state sinks to the end.
    held = {t["bucket"] for t in tickets}
    buckets = [b for b in bucket_order if b in held and b != NO_STATE_BUCKET]
    if NO_STATE_BUCKET in held:
        buckets.append(NO_STATE_BUCKET)
    tickets.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return {
        "sources": listed_sources,
        "source_labels": source_labels,
        "buckets": buckets,
        "bucket_meta": {b: bucket_meta[b] for b in buckets if b in bucket_meta},
        "done_buckets": sorted(done_buckets & set(buckets)),
        "ingest_states": ingest_states,
        "tickets": tickets,
        "errors": errors,
    }


async def find_ticket(source: str, ticket_id: str):
    """The live provider record for one ticket on one configured source."""
    from backend.ticket_ingestion.providers import get_provider

    cfg = _load_config()
    for src in cfg.ticketing_sources or []:
        if (src.id or src.provider) == source:
            provider = get_provider(src)
            story = await provider.fetch(str(ticket_id))
            story.repo_url = src.repo_url
            story.agent = getattr(src, "agent", "")
            # The source this ticket launches under, so the post-launch move
            # (start_state) resolves the same card the row was started from —
            # two sources of the same provider share `provider` and nothing else.
            story.source_key = source
            return story
    raise LookupError(
        f"No ticketing source {source!r} is configured — " "check Intake → Tickets"
    )


def build_prompt(story) -> str:
    """The ticket's session prompt — the pipeline's own prompt builder plus
    the deferred-attachments note ``SessionRunner`` adds (the workspace is
    created during launch, so attachments land just after the session starts).
    """
    from backend.ticket_ingestion.claude_runner import ClaudeCodeRunner

    prompt = ClaudeCodeRunner(_load_config())._build_prompt(story, None, None)
    if story.attachments:
        names = ", ".join(a.name for a in story.attachments if getattr(a, "name", None))
        prompt += (
            "\n\n## Attached Files\n\n"
            f"This ticket has {len(story.attachments)} attachment(s)"
            + (f" ({names})" if names else "")
            + ". They are being downloaded into `.ticket_attachments/` in "
            "this workspace and should appear within a few seconds — check "
            "that directory (re-list if it is empty at first) and read them "
            "as part of the ticket context.\n"
        )
    return prompt


async def download_attachments(inst, story) -> None:
    """Best-effort post-launch attachment drop into the live workspace —
    the forced-path twin of ``SessionRunner._post_start``."""
    if not story.attachments:
        return
    from backend.ticket_ingestion.claude_runner import ClaudeCodeRunner

    try:
        wp = inst.GetWorktreePath()
        if not wp:
            return
        await ClaudeCodeRunner(_load_config())._download_attachments(Path(wp), story)
    except Exception as err:  # noqa: BLE001
        _logger.warning(
            "Attachment download for forced ticket %s failed (continuing): %s",
            story.slug,
            err,
        )


async def move_to_start_state(story) -> str:
    """Move a force-started ticket into its source's configured start state.

    The web twin of what ``SessionRunner`` does after a pipeline launch, and
    literally the same code: a ticket started by hand from the panel has to land
    on the board exactly where one the pipeline picked up does, or the column
    stops meaning "being worked on". Best-effort — see
    :mod:`backend.ticket_ingestion.start_state`.
    """
    from backend.ticket_ingestion.start_state import move_started

    return await move_started(story, _load_config())


def record_started(story) -> None:
    """In-flight ledger marker — from here concurrent pipeline scans treat the
    ticket as taken (same guard as the orchestrator's). A team run's
    reservation for it becomes this marker in place (never two entries)."""
    from backend.ticket_ingestion.models import ProcessingRecord
    from backend.ticket_ingestion.state import (
        claim_reservation,
        record_processed_story,
    )

    if claim_reservation(_REPO_ROOT, story.slug):
        return

    record_processed_story(
        _REPO_ROOT,
        ProcessingRecord(
            story_id=story.slug,
            branch=story.slug,
            status="in_flight",
            processed_at=datetime.now(timezone.utc),
        ),
    )


def record_result(story, branch: str | None = None, error: str | None = None) -> None:
    """Flip the in-flight marker to its terminal status (completed / failed) —
    same semantics as the pipeline, including the manual unblock (delete the
    state.json entry to allow a re-run)."""
    from backend.ticket_ingestion.state import update_processed_story

    update_processed_story(
        _REPO_ROOT,
        story.slug,
        status="failed" if error else "completed",
        branch=branch,
        failure_reason=error,
    )


# --------------------------------------------------------------------------- #
# Launching a ticket session (the Intake panel's "Begin work", the MCP's
# spawn_ticket_session, and a team run's ticket task)
# --------------------------------------------------------------------------- #
class LaunchError(Exception):
    """A ticket start refused before anything launched: ``status`` and
    ``body`` are exactly what the route answers with."""

    def __init__(self, status: int, body: dict) -> None:
        super().__init__(str(body.get("error") or status))
        self.status = status
        self.body = body


_NO_LINEAGE = {"parent": "", "spawned": False, "report_back": False, "note": ""}


async def launch(
    source: str,
    ticket_id: str,
    *,
    title: Optional[str] = None,
    branch: Optional[str] = None,
    depth: str = "",
    agent: str = "",
    effort: str = "",
    lineage: Optional[dict] = None,
    extra_prompt: str = "",
    run_id: str = "",
) -> dict:
    """Start a coding session for one ticket and return the 202 body.

    The body of ``POST /api/tickets/start``, callable without HTTP: the route
    validates its payload and calls this; a team run calls it for each ticket
    task. The session is registered and provisioned on a background task (the
    engine owns provisioning, inside ``Instance.Start``), so this returns as
    soon as the title is claimed in the pending registry; a launch failure is
    reported as ``session.create_failed`` plus ``/api/create_failures``.

    * ``title`` / ``branch`` override the ticket's own (a team run's "retry
      fresh" starts ``<title>-2`` on a new branch and keeps the old one).
    * ``depth``: ``""`` = the source's configured rung, ``"off"`` = do not arm
      (the caller arms its own lane), else that rung.
    * ``agent`` / ``effort``: per-start overrides of the source's chain.
    * ``lineage``: ``{parent, spawned, report_back, note}`` from
      ``server._intake_lineage`` (all-empty for the Intake panel and runs).
    * ``extra_prompt``: appended after the ticket text (a run's brief).

    Raises :class:`LaunchError` (404 unknown ticket / source, 409 a session
    for it already exists, 502 the tracker failed).
    """
    srv = _server()
    lineage = dict(lineage or _NO_LINEAGE)
    agent_override = str(agent or "")
    effort_override = str(effort or "")
    depth_override = str(depth or "")
    # Row first, provider fetch second (see the PR endpoint). The panel's
    # cached list is the title's source: ticket slugs are provider-defined
    # (Shortcut hardcodes sc-<id>), so deriving one here would be a second
    # implementation waiting to drift.
    early = title or srv._cached_session_title(
        srv._ASSIGNED_TICKETS_CACHE,
        "tickets",
        lambda t: t.get("source") == source and str(t.get("id")) == ticket_id,
    )
    if early:
        if early in srv.ENGINE.instances or srv._pending_has(early):
            raise LaunchError(
                409,
                {
                    "error": "session %s already exists — close it to re-run" % early,
                    "title": early,
                },
            )
        srv._pending_add(early, "tix")

    try:
        story = await find_ticket(source, ticket_id)
    except LookupError as err:
        srv._pending_drop(early)
        raise LaunchError(404, {"error": str(err)}) from None
    except Exception as err:  # noqa: BLE001
        srv._pending_drop(early)
        raise LaunchError(502, {"error": str(err)}) from None

    # A per-start choice outranks the source's card. Stamped onto the story
    # because that is the field every launch path already consults first.
    if agent_override:
        story.agent = agent_override

    title = title or session_title(story)
    if title != early:
        srv._pending_drop(early)  # stale cache entry — keep only the real title
        if title in srv.ENGINE.instances or srv._pending_has(title):
            raise LaunchError(
                409,
                {
                    "error": "session %s already exists — close it to re-run" % title,
                    "title": title,
                },
            )
    # Another of the user's devices may already be on it (fleet_claims) — the
    # one guard the local checks above can't see.
    from backend.web.core import fleet_claims as _fleet_claims

    elsewhere = await _fleet_claims.holder(title, fresh=True)
    if elsewhere:
        srv._pending_drop(early)
        srv._pending_drop(title)
        raise LaunchError(
            409,
            {
                "error": "%s is already %s"
                % (title, _fleet_claims.describe(elsewhere)),
                "title": title,
                "elsewhere": elsewhere,
            },
        )
    branch = branch or branch_for(story)
    # The branch is known now, so the row can read as the ticket it is rather
    # than a bare slug (add() keeps the original `since`).
    srv._pending_add(
        title,
        "tix",
        branch=branch,
        workspace_strategy=workspace_mode(),
        parent=lineage["parent"],
        spawned=lineage["spawned"],
    )
    srv._arm_intake_autopilot(
        title,
        depth_override or srv._source_intake_depth(source),
        "tix",
        str(getattr(story, "id", "") or ticket_id),
        message=str(getattr(story, "name", "") or ""),
        by=("agent:" + lineage["parent"]) if lineage.get("parent") else "user",
    )
    # The ticket's source may pin its own agent CLI; empty falls back to this
    # app's default program. Resolved before the 202 (not in the launch) so an
    # agent-spawned start can be told whether its worker gets the report-back
    # footer — that depends on which CLI it runs.
    program = agent_for(story) or srv.ENGINE.default_program()
    prompt_tail, report_back, report_reason = srv._intake_prompt_tail(
        lineage, title, program
    )
    if extra_prompt:
        prompt_tail = "\n\n" + extra_prompt.strip() + prompt_tail

    async def _bg_start() -> None:
        # Same shape as the pipeline's SessionRunner.run, but against THIS
        # server's engine so the session shows up in the grid without a
        # reload. The engine owns provisioning (inside Instance.Start), so
        # unlike the PR path there is no pre-provision step.
        marked = False
        try:
            prompt = build_prompt(story)
            # A previous run of this ticket may have left a worktree holding the
            # branch: the session is long gone (nothing blocked the button) but
            # git still refuses to check the branch out twice. Reclaim it when
            # nothing owns it and it holds no work — otherwise the provisioning
            # error stands, unchanged.
            await asyncio.to_thread(
                srv._worktree_reclaim.reclaim_for_launch,
                getattr(story, "repo_url", "") or "",
                branch,
            )
            # In-flight ledger marker BEFORE the slow launch, so a running
            # pipeline's scans treat the ticket as taken (orchestrator guard).
            record_started(story)
            marked = True
            # `program` was resolved above, before the options, because this
            # start's effort has to be translated into THAT CLI's spelling.
            # This start's own rung wins; the source's default is what applies
            # when the row did not pick one. Resolved here rather than in
            # `_start_effort_override` because the source is only knowable once
            # the story is — and an explicit choice on the row must be able to
            # ask for LESS thinking than the queue's default, not just more.
            level = effort_override or effort_for(story)
            prompt = srv._provider_effort.decorate_prompt(prompt, program, level)
            # Red zones + the repo's Plan-first flag (worktree not cut yet:
            # key off the source repo's local checkout / base clone).
            prompt = await asyncio.to_thread(
                lambda p=prompt: srv._red_zone_prompt(
                    p,
                    program,
                    srv._repo_url_workdirs(getattr(story, "repo_url", "") or ""),
                    None,
                )
            )
            # An agent-spawned worker's note + report-back footer go LAST, so
            # the footer is the final thing it reads (as spawn_session's is).
            prompt += prompt_tail
            inst = srv.session.NewInstance(
                srv.session.InstanceOptions(
                    title=title,
                    path=".",
                    program=program,
                    provisioned=True,
                    workspace_strategy=workspace_mode(),
                    new_branch=branch,
                    prompt=prompt,
                    provision_repo_url=getattr(story, "repo_url", "") or "",
                    launch_args=srv._start_launch_args(program, level),
                    parent=lineage["parent"],
                    spawned=lineage["spawned"],
                )
            )
            inst.ExtraEnv = srv._ports.env_for(title)
            inst.SetStatus(srv.Loading)
            with srv.ENGINE.lock:
                # Re-checked under the claim, like the create route: the
                # parent may have gone, or a concurrent spawn taken the slot.
                claim_err = srv._intake_claim_error(lineage)
                if claim_err:
                    raise RuntimeError(claim_err)
                srv.ENGINE.instances[title] = inst
            if lineage["parent"]:
                # The spawn record in the parent's Thread tab.
                srv._thread.note_seed(title, srv._created_epoch(inst), prompt)
            srv._seed_event_snapshot(title)
            created_data = {
                "program": inst.Program,
                "provisioned": True,
                "ticket": str(story.id),
            }
            if lineage["parent"]:
                created_data["parent"] = lineage["parent"]
            if lineage["spawned"]:
                created_data["spawned"] = True
            if run_id:
                created_data["run"] = run_id
            srv._events.BUS.emit(
                "session.created",
                session=title,
                new="loading",
                data=created_data,
            )
            try:
                await asyncio.to_thread(inst.Start, True)
                srv.ENGINE.save()
            except Exception:
                # By identity: this task may be the loser of a re-start, and
                # popping by name would delete the LIVE session's record.
                srv._drop_failed_start(title, inst)
                raise
            # Terminal ledger entry so auto ingestion doesn't run it again.
            record_result(story, branch=branch)
            await download_attachments(inst, story)
            # …and move the ticket on its board, if its source asked for that.
            # After the launch, like the attachments above: the state means "a
            # session is working on this", which only became true just now.
            moved = await move_to_start_state(story)
            if moved and srv.log.InfoLog is not None:
                srv.log.InfoLog.Printf(
                    "ticket %s moved to its source's start state (%s)", title, moved
                )
            if srv.log.InfoLog is not None:
                srv.log.InfoLog.Printf("forced ticket session %s live", title)
        except Exception as err:  # noqa: BLE001
            if marked:
                record_result(story, error=str(err))
            if srv.log.ErrorLog is not None:
                srv.log.ErrorLog.Printf(
                    "forced ticket session %s failed: %v", title, err
                )
            # The reason a caller polling the listing can ask for (the row
            # just vanishes otherwise) — GET /api/create_failures.
            srv._note_create_failure(title, str(err))
            srv._events.BUS.emit(
                "session.create_failed", session=title, data={"error": str(err)}
            )
        finally:
            srv._pending_drop(title)

    srv._register_task(_bg_start())
    body = {"started": True, "title": title}
    if lineage["parent"] or lineage["spawned"] or lineage["report_back"]:
        # What an agent-spawned start needs to report about its worker.
        body.update(
            {
                "branch": branch,
                "program": program,
                "parent": lineage["parent"],
                "spawned": lineage["spawned"],
                "report_back": report_back,
            }
        )
        if lineage["report_back"] and not report_back:
            body["reason"] = report_reason
    return body


def ledger_holder(slug: str) -> Optional[str]:
    """Who holds ``slug`` in flight in the ingestion ledger: ``""`` for a
    started session (the pipeline's, a launch's), ``"run:<id>"`` for a team
    run's reservation, None when it is not in flight."""
    from backend.ticket_ingestion.state import latest_story_entry

    entry = latest_story_entry(_REPO_ROOT, slug)
    if not entry or entry.get("status") != "in_flight":
        return None
    return str(entry.get("reserved_by") or "")


def reserve(slug: str, by: str = "") -> bool:
    """Record ``slug`` ``in_flight`` in the ingestion ledger NOW, before any
    session exists — a team run reserves its queued tickets so the pipeline's
    scans skip them (the same guard :func:`record_started` sets at launch).
    ``by`` (``"run:<id>"``) tags the reservation so only that holder hands it
    back. Returns False — and records nothing — when the ticket is already in
    flight for someone else (the pipeline started it, or another run holds
    it): the caller must not start it too."""
    from backend.ticket_ingestion.models import ProcessingRecord
    from backend.ticket_ingestion.state import record_processed_story

    holder = ledger_holder(slug)
    if holder is not None:
        return bool(by) and holder == by
    record_processed_story(
        _REPO_ROOT,
        ProcessingRecord(
            story_id=slug,
            branch=slug,
            status="in_flight",
            processed_at=datetime.now(timezone.utc),
            reserved_by=by or None,
        ),
    )
    return True


def release_reservation(slug: str, by: str = "") -> bool:
    """Undo :func:`reserve` for a ticket that never launched (a team run
    cancelled while it was still queued): drop that one ``in_flight`` ledger
    entry so auto ingestion may pick it up again. Only the reservation ``by``
    made is dropped; history (an earlier completed/failed run of the ticket)
    and anyone else's marker are left alone."""
    from backend.ticket_ingestion.state import remove_in_flight_story

    return remove_in_flight_story(_REPO_ROOT, slug, reserved_by=by or None)
