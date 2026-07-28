"""Force-start ticket sessions from the web UI (Settings → Ticketing).

The automated pipeline (``backend.ticket_ingestion``) only picks up
tickets that are (a) assigned to the configured user and updated since the
per-source poll checkpoint, (b) absent from state.json's
``processed_stories`` ledger and (c) without an existing
``feature/<slug>/…`` branch on the remote — so a ticket can silently never
get ingested (most commonly: it was recorded as failed/skipped once, or its
branch was pushed by hand). This module backs the Settings → Ticketing
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

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

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


def branch_for(story) -> str:
    """The ``feature/<slug>/<name-slug>`` branch the pipeline would push for
    ``story`` — the pipeline's own naming, so a forced start and an auto ingest
    of the same ticket land on the same branch."""
    from backend.ticket_ingestion.provisioner import _branch_name_for

    return _branch_name_for(story)


def skip_reasons(
    story,
    ledger: dict,
    pending_ids: set,
    branches: set,
    member_ids,
    ingest_state: list | None = None,
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
    annotate it already filtered server-side).
    """
    from backend.ticket_ingestion.filter import AssigneeFilter

    reasons: list[str] = []
    if ingest_state and story.state and story.state not in ingest_state:
        reasons.append("not in an ingest state — won't auto-ingest")
    status = ledger.get(story.slug) or ledger.get(str(story.id))
    if status:
        label = {
            "completed": "already ingested (recorded completed in the ledger)",
            "in_flight": "a session for it is in flight",
            "failed": "failed earlier — delete its state.json ledger entry to retry",
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
        load_processed_story_statuses,
    )

    cfg = _load_config()
    sources = cfg.ticketing_sources or []
    ledger = load_processed_story_statuses(_REPO_ROOT)
    pending_ids = {e.get("story_id") for e in load_pending_stories(_REPO_ROOT)}

    tickets: list[dict] = []
    errors: list[dict] = []
    listed_sources: list[str] = []
    bucket_order: list[str] = []
    done_buckets: set = set()
    ingest_states: dict = {}  # source key -> configured ingest bucket name
    branch_cache: dict[str, set] = {}
    for src in sources:
        source_key = src.id or src.provider
        try:
            provider = get_provider(src)
            stories = await provider.search_assigned_all()
        except Exception as err:  # noqa: BLE001 — token / network / config
            errors.append({"source": source_key, "error": str(err)})
            continue
        listed_sources.append(source_key)
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
        except Exception:  # noqa: BLE001
            pass
        # The source's configured ingest filter (one or more states), as
        # bucket names — tickets in any other bucket must not read as
        # "queued for auto ingestion".
        from backend.ticket_ingestion.providers.base import workflow_state_list

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
        member_ids = [src.member_id] if src.member_id else []
        for story in stories:
            story.repo_url = src.repo_url
            reasons = skip_reasons(
                story,
                ledger,
                pending_ids,
                branch_cache[repo],
                member_ids,
                ingest_state=ingest_state,
            )
            bucket = story.state or NO_STATE_BUCKET
            if bucket not in bucket_order:
                bucket_order.append(bucket)
            tickets.append(
                {
                    "source": source_key,
                    "source_label": getattr(provider, "label", "") or source_key,
                    "id": str(story.id),
                    "slug": story.slug,
                    "name": story.name,
                    "url": story.app_url,
                    "created_at": story.created_at.isoformat(),
                    "session": session_title(story),
                    "bucket": bucket,
                    "eligible": not reasons,
                    "reasons": reasons,
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
        "buckets": buckets,
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
            return story
    raise LookupError(
        f"No ticketing source {source!r} is configured — " "check Settings → Ticketing"
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


def record_started(story) -> None:
    """In-flight ledger marker — from here concurrent pipeline scans treat the
    ticket as taken (same guard as the orchestrator's)."""
    from backend.ticket_ingestion.models import ProcessingRecord
    from backend.ticket_ingestion.state import record_processed_story

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
