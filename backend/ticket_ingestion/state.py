"""State persistence for the pipeline (state.json)."""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.ticket_ingestion.models import (
    ProcessedIssue,
    ProcessedPR,
    ProcessingRecord,
)

_logger = logging.getLogger(__name__)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_STATE_FILENAME = "state.json"


def _state_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / _STATE_FILENAME


def _read_state(state_dir: Path | str) -> dict:
    path = _state_path(state_dir)
    if not path.exists():
        return {}
    try:
        text = path.read_text()
        if not text.strip():
            return {}
        data = json.loads(text)
    except json.JSONDecodeError as e:
        # A corrupt ledger means every past ticket/PR would re-ingest. Move
        # the bytes aside for recovery instead of silently starting over.
        backup = path.with_name(
            "{}.corrupt-{}".format(path.name, time.strftime("%Y%m%d-%H%M%S"))
        )
        try:
            os.replace(path, backup)
            _logger.error(
                "State file %s is corrupt (%s); preserved at %s — "
                "processed-story/PR history is lost until restored",
                path,
                e,
                backup,
            )
        except OSError:
            _logger.error(
                "State file %s is corrupt (%s) and could not be backed up", path, e
            )
        return {}
    except OSError as e:
        _logger.warning("Failed to read state file %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_state(state_dir: Path | str, data: dict) -> None:
    # Atomic replace: a crash mid-write must not truncate the ledger (the
    # singleton flock in __main__ already serializes writers cross-process).
    # fsync before replace so a power loss can't promote a truncated tmp file.
    path = _state_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(data, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_last_run_timestamp(state_dir: Path | str, key: str) -> datetime:
    """Last successful scan time for the ticketing source keyed by ``key``."""
    data = _read_state(state_dir)
    per = data.get("last_run_timestamps")
    raw = per.get(key) if isinstance(per, dict) else None
    if not isinstance(raw, str):
        return _EPOCH
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        _logger.warning("Invalid timestamp in state file: %r", raw)
        return _EPOCH
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def save_last_run_timestamp(state_dir: Path | str, ts: datetime, key: str) -> None:
    data = _read_state(state_dir)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    per = data.get("last_run_timestamps")
    if not isinstance(per, dict):
        per = {}
    per[key] = ts.isoformat()
    data["last_run_timestamps"] = per
    if not isinstance(data.get("processed_stories"), list):
        data["processed_stories"] = []
    _write_state(state_dir, data)


def load_processed_story_ids(state_dir: Path | str) -> set:
    """Return the set of provider-scoped ticket slugs (e.g. ``"sc-123"``,
    ``"jira-PROJ-1"``) already processed.

    Entries are included regardless of ``status`` — in particular an
    ``in_flight`` entry (recorded the moment a session starts, see
    :func:`update_processed_story`) blocks re-ingestion while the session is
    still running, closing the scan-during-long-session duplicate window."""
    data = _read_state(state_dir)
    stories = data.get("processed_stories")
    if not isinstance(stories, list):
        return set()
    ids: set = set()
    for entry in stories:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("story_id")
        if isinstance(sid, str) and sid:
            ids.add(sid)
    return ids


def load_processed_story_statuses(state_dir: Path | str) -> dict:
    """Slug -> latest ledger status (``completed`` / ``in_flight`` / ``skipped``
    / ``failed``) for every processed story.

    The status-annotated companion to :func:`load_processed_story_ids` — used
    by the web UI's force-start panel to say *why* a ticket won't auto-ingest,
    not just that it won't. The last entry for a slug wins (matching
    :func:`update_processed_story`, which updates the most recent one)."""
    data = _read_state(state_dir)
    stories = data.get("processed_stories")
    if not isinstance(stories, list):
        return {}
    out: dict = {}
    for entry in stories:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("story_id")
        if isinstance(sid, str) and sid:
            out[sid] = str(entry.get("status") or "")
    return out


def load_processed_story_failures(state_dir: Path | str) -> dict:
    """Slug -> the ``failure_reason`` on its latest ``failed`` ledger entry.

    The companion to :func:`load_processed_story_statuses`, which returns only
    the status. The reason is the one string that says what to DO about a
    failure — "branch '…' is already checked out at <path>", say — and it sat
    unread in state.json while the UI offered a generic "delete the ledger entry
    to retry" remedy that clears the *record* of the failure and not its cause.
    Same last-entry-wins rule as the status loader.
    """
    data = _read_state(state_dir)
    stories = data.get("processed_stories")
    if not isinstance(stories, list):
        return {}
    out: dict = {}
    for entry in stories:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("story_id")
        if not (isinstance(sid, str) and sid):
            continue
        if entry.get("status") == "failed":
            out[sid] = str(entry.get("failure_reason") or "")
        else:
            # A later success supersedes an earlier failure's reason.
            out.pop(sid, None)
    return out


def record_processed_story(state_dir: Path | str, record: ProcessingRecord) -> None:
    data = _read_state(state_dir)
    stories = data.get("processed_stories")
    if not isinstance(stories, list):
        stories = []

    entry: dict = {
        "story_id": record.story_id,
        "branch": record.branch,
        "status": record.status,
        "processed_at": record.processed_at.isoformat(),
    }
    if record.failure_reason is not None:
        entry["failure_reason"] = record.failure_reason

    stories.append(entry)
    data["processed_stories"] = stories
    _write_state(state_dir, data)


def update_processed_story(
    state_dir: Path | str,
    story_id: int | str,
    *,
    status: str,
    branch: str | None = None,
    failure_reason: str | None = None,
    processed_at: datetime | None = None,
) -> bool:
    """Update the most recent ``processed_stories`` entry for ``story_id``.

    Second half of the in-flight idempotency handshake: the orchestrator
    appends an ``in_flight`` record via :func:`record_processed_story` the
    moment work on a story starts (which immediately blocks re-ingestion by
    :func:`load_processed_story_ids`), then calls this to flip that same entry
    to ``completed`` / ``skipped`` / ``failed`` when the session ends.
    ``branch`` and ``failure_reason`` are only written when provided;
    ``processed_at`` defaults to now.

    Returns ``True`` when an existing entry was updated in place. When no
    entry exists (e.g. a hand-edited ledger) a full record is appended instead
    and ``False`` is returned, so a terminal status is never lost.

    Crash behavior: if the pipeline dies mid-session the entry stays
    ``in_flight`` and keeps blocking re-ingestion until the startup reaper
    (:func:`reap_stale_in_flight`) flips it to ``failed`` — after which the
    manual unblock is the same as ever: delete the entry from state.json's
    ``processed_stories`` list (mirrors the documented ``processed_prs``
    unblock in :func:`load_processed_prs`).
    """
    if processed_at is None:
        processed_at = datetime.now(timezone.utc)
    data = _read_state(state_dir)
    stories = data.get("processed_stories")
    if not isinstance(stories, list):
        stories = []

    for entry in reversed(stories):
        if isinstance(entry, dict) and entry.get("story_id") == story_id:
            entry["status"] = status
            entry["processed_at"] = processed_at.isoformat()
            if branch is not None:
                entry["branch"] = branch
            if failure_reason is not None:
                entry["failure_reason"] = failure_reason
            data["processed_stories"] = stories
            _write_state(state_dir, data)
            return True

    # No prior record (manual edit): append a full one instead.
    entry = {
        "story_id": story_id,
        "branch": branch if branch is not None else str(story_id),
        "status": status,
        "processed_at": processed_at.isoformat(),
    }
    if failure_reason is not None:
        entry["failure_reason"] = failure_reason
    stories.append(entry)
    data["processed_stories"] = stories
    _write_state(state_dir, data)
    return False


def reap_stale_in_flight(
    state_dir: Path | str,
    is_alive=None,
    max_age_seconds: float = 24 * 60 * 60,
    now: datetime | None = None,
) -> list:
    """Flip crashed-mid-session ``in_flight`` entries to ``failed`` at startup.

    A crash between the in-flight marker and the terminal update used to leave
    the entry ``in_flight`` forever — indistinguishable from a running session
    and never retried without hand-editing state.json. ``is_alive(story_id)``
    is the caller's liveness probe (e.g. ``tmux has-session`` on the derived
    session names) returning True (running, keep), False (dead, reap) or None
    (unknown). When liveness is unknown the entry is only reaped once its
    ``processed_at`` (stamped when the marker was written) is older than
    ``max_age_seconds`` — conservative, so a live-but-unprobeable session is
    never falsely failed. Returns the reaped story ids.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    data = _read_state(state_dir)
    stories = data.get("processed_stories")
    if not isinstance(stories, list):
        return []
    reaped: list = []
    for entry in stories:
        if not isinstance(entry, dict) or entry.get("status") != "in_flight":
            continue
        sid = entry.get("story_id")
        alive = None
        if is_alive is not None:
            try:
                alive = is_alive(str(sid))
            except Exception as e:  # noqa: BLE001
                _logger.warning("Liveness probe failed for %s: %s", sid, e)
                alive = None
        if alive is True:
            continue
        if alive is None:
            raw = entry.get("processed_at")
            try:
                started = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                _logger.warning(
                    "in_flight entry %s has unparseable processed_at %r; leaving it",
                    sid,
                    raw,
                )
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (now - started).total_seconds() <= max_age_seconds:
                continue
        entry["status"] = "failed"
        entry["failure_reason"] = (
            "stale in_flight reaped at startup (no live session found)"
        )
        entry["processed_at"] = now.isoformat()
        reaped.append(sid)
        _logger.warning(
            "Reaped stale in_flight story %s -> failed (crashed mid-session; "
            "delete its state.json entry to re-run it)",
            sid,
        )
    if reaped:
        data["processed_stories"] = stories
        _write_state(state_dir, data)
    return reaped


def load_pending_stories(state_dir: Path | str) -> list[dict]:
    """Entries enqueued by a scan but not yet picked up by ``process_story``.

    Written at enqueue time (before the poll checkpoint advances) so a crash
    with a non-empty in-memory queue doesn't lose those tickets forever; the
    orchestrator re-enqueues them on startup."""
    data = _read_state(state_dir)
    pending = data.get("pending_stories")
    if not isinstance(pending, list):
        return []
    return [e for e in pending if isinstance(e, dict) and e.get("story_id")]


def record_pending_story(
    state_dir: Path | str,
    story_id: int | str,
    ticket_id: int | str,
    source_key: str | None,
    enqueued_at: datetime | None = None,
) -> None:
    """Upsert an enqueued-but-not-yet-processed marker for ``story_id``."""
    if enqueued_at is None:
        enqueued_at = datetime.now(timezone.utc)
    data = _read_state(state_dir)
    pending = data.get("pending_stories")
    if not isinstance(pending, list):
        pending = []
    pending = [
        e
        for e in pending
        if not (isinstance(e, dict) and e.get("story_id") == story_id)
    ]
    pending.append(
        {
            "story_id": story_id,
            "ticket_id": str(ticket_id),
            "source_key": source_key,
            "enqueued_at": enqueued_at.isoformat(),
        }
    )
    data["pending_stories"] = pending
    _write_state(state_dir, data)


def remove_pending_story(state_dir: Path | str, story_id: int | str) -> None:
    """Drop the pending marker once ``process_story`` picks the story up."""
    data = _read_state(state_dir)
    pending = data.get("pending_stories")
    if not isinstance(pending, list):
        return
    kept = [
        e
        for e in pending
        if not (isinstance(e, dict) and e.get("story_id") == story_id)
    ]
    if len(kept) == len(pending):
        return
    data["pending_stories"] = kept
    _write_state(state_dir, data)


def load_processed_prs(state_dir: Path | str) -> set[tuple[str, int]]:
    """Return the set of ``(repo, number)`` pairs already provisioned.

    Repo-scoped so PR #5 in one repo doesn't mask PR #5 in another (multi-repo
    review).
    """
    data = _read_state(state_dir)
    prs = data.get("processed_prs")
    if not isinstance(prs, list):
        return set()
    out: set[tuple[str, int]] = set()
    for entry in prs:
        if isinstance(entry, dict) and isinstance(entry.get("number"), int):
            repo = entry.get("repo")
            out.add((repo if isinstance(repo, str) else "", entry["number"]))
    return out


def record_processed_pr(state_dir: Path | str, record: ProcessedPR) -> None:
    data = _read_state(state_dir)
    prs = data.get("processed_prs")
    if not isinstance(prs, list):
        prs = []
    entry: dict = {
        "number": record.number,
        "head_sha": record.head_sha,
        "processed_at": record.processed_at.isoformat(),
    }
    if record.repo:
        entry["repo"] = record.repo
    if record.status:
        entry["status"] = record.status
    prs.append(entry)
    data["processed_prs"] = prs
    _write_state(state_dir, data)


def load_processed_issues(state_dir: Path | str) -> set[tuple[str, int]]:
    """Return the set of ``(repo, number)`` issue pairs already handled.

    Repo-scoped like :func:`load_processed_prs`; manual unblock is the same —
    delete the entry from state.json's ``processed_issues`` list.
    """
    data = _read_state(state_dir)
    issues = data.get("processed_issues")
    if not isinstance(issues, list):
        return set()
    out: set[tuple[str, int]] = set()
    for entry in issues:
        if isinstance(entry, dict) and isinstance(entry.get("number"), int):
            repo = entry.get("repo")
            out.add((repo if isinstance(repo, str) else "", entry["number"]))
    return out


def record_processed_issue(state_dir: Path | str, record: ProcessedIssue) -> None:
    data = _read_state(state_dir)
    issues = data.get("processed_issues")
    if not isinstance(issues, list):
        issues = []
    entry: dict = {
        "number": record.number,
        "processed_at": record.processed_at.isoformat(),
    }
    if record.repo:
        entry["repo"] = record.repo
    if record.status:
        entry["status"] = record.status
    issues.append(entry)
    data["processed_issues"] = issues
    _write_state(state_dir, data)


def _issue_attempt_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def record_issue_attempt(state_dir: Path | str, repo: str, number: int) -> int:
    """Count a failed provisioning/launch attempt for an issue; returns the
    new total. Mirrors :func:`record_pr_attempt` so an issue that keeps
    failing is capped instead of re-cloned on every poll forever."""
    data = _read_state(state_dir)
    attempts = data.get("issue_attempts")
    if not isinstance(attempts, dict):
        attempts = {}
    key = _issue_attempt_key(repo, number)
    entry = attempts.get(key)
    count = entry.get("count", 0) + 1 if isinstance(entry, dict) else 1
    attempts[key] = {
        "count": count,
        "last_attempt": datetime.now(timezone.utc).isoformat(),
    }
    data["issue_attempts"] = attempts
    _write_state(state_dir, data)
    return count


def clear_issue_attempts(state_dir: Path | str, repo: str, number: int) -> None:
    data = _read_state(state_dir)
    attempts = data.get("issue_attempts")
    if not isinstance(attempts, dict):
        return
    if attempts.pop(_issue_attempt_key(repo, number), None) is None:
        return
    data["issue_attempts"] = attempts
    _write_state(state_dir, data)


def _pr_attempt_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def record_pr_attempt(state_dir: Path | str, repo: str, number: int) -> int:
    """Count a failed provisioning/launch attempt for a PR; returns the new
    total. Mirrors the story ``failed`` pattern so a PR that keeps failing is
    capped instead of re-cloned on every poll forever."""
    data = _read_state(state_dir)
    attempts = data.get("pr_attempts")
    if not isinstance(attempts, dict):
        attempts = {}
    key = _pr_attempt_key(repo, number)
    entry = attempts.get(key)
    count = entry.get("count", 0) + 1 if isinstance(entry, dict) else 1
    attempts[key] = {
        "count": count,
        "last_attempt": datetime.now(timezone.utc).isoformat(),
    }
    data["pr_attempts"] = attempts
    _write_state(state_dir, data)
    return count


def clear_pr_attempts(state_dir: Path | str, repo: str, number: int) -> None:
    data = _read_state(state_dir)
    attempts = data.get("pr_attempts")
    if not isinstance(attempts, dict):
        return
    if attempts.pop(_pr_attempt_key(repo, number), None) is None:
        return
    data["pr_attempts"] = attempts
    _write_state(state_dir, data)
