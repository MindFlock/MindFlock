"""Force-start PR reviews from the web UI (Intake → Pull requests).

The automated monitor (``backend.ticket_ingestion.pr_monitor``) only picks
up open PRs that are (a) authored by the token's own user, (b) past the
min-age grace period and (c) absent from state.json's ``processed_prs``
ledger — so a PR can silently never get reviewed (most commonly: it was
recorded as processed back when it had no actionable comments yet, and new
comments never re-trigger it). This module backs the Intake → Pull requests
tab: it lists every non-draft open PR on the watched repos annotated with
*why* auto review is or isn't taking it, and force-starts a review session
for any of them, bypassing those filters.

The forced path reuses the pipeline's own pieces (comment fetch, PR workspace
provisioner, consolidated prompt) but runs inside the web server process, so
it works even while ingestion is stopped. ``server.py`` owns the engine side
(registering the session in the live grid).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

_logger = logging.getLogger(__name__)


def _resolve_repo_root() -> Path:
    """Where the pipeline keeps state.json / workspaces/. ``parents[4]`` only
    lands on the repo root for a src-layout dev checkout; for an installed copy
    (``uv tool install`` → site-packages) it is the interpreter lib dir, which
    splits the PR ledger. Same resolution order as the ingestion addon:
    ``MINDFLOCK_REPO_ROOT`` env → nearest ancestor with ``config.toml`` → cwd.
    """
    env = (os.environ.get("MINDFLOCK_REPO_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "config.toml").is_file():
            return parent
    return Path.cwd()


# The pipeline runs with cwd = repo root and keeps state.json / workspaces/
# there; this server's cwd is not guaranteed to match, so every relative path
# is anchored here explicitly.
_REPO_ROOT = _resolve_repo_root()


def _load_config():
    """The pipeline's layered config (env → settings.json → config.toml).

    Raises ``ConfigError`` when ingestion has never been configured — callers
    surface the message verbatim. The relative ``workspace_dir`` default is
    re-anchored at the repo root so a differing server cwd can't scatter PR
    workspaces.
    """
    from backend.ticket_ingestion.config import load_config

    cfg = load_config()
    if not Path(cfg.workspace_dir).is_absolute():
        cfg.workspace_dir = _REPO_ROOT / cfg.workspace_dir
    return cfg


def review_agent(repo: str = "") -> str:
    """The coding CLI a PR-review session should run, ``""`` = the app default.

    Reads the SAME chain the automated pipeline does
    (:meth:`PipelineConfig.pr_agent` — the repo's own card, then Intake → Pull
    requests' screen-wide Agent CLI stored as ``github.agent``, then
    ``[mindflock].agent``). The forced-review route used to launch
    ``ENGINE.default_program()`` outright, so the Agent CLI dropdown sitting
    directly above the "Begin review" button governed only the auto monitor:
    clicking Begin review launched the app-wide default instead of the provider
    the user had picked for reviews.

    Best-effort by design: this is a launch path, and an unconfigured or
    unreadable ingestion config should fall through to the app default rather
    than block the review.
    """
    try:
        cfg = _load_config()
    except Exception:  # noqa: BLE001 — ingestion may not be configured at all
        return ""
    fn = getattr(cfg, "pr_agent", None)
    try:
        return (fn(repo) if callable(fn) else "") or ""
    except Exception:  # noqa: BLE001
        return ""


def session_title(pr) -> str:
    """Engine session title for a PR review — ``pr-<repo-name>-<number>`` (via
    :func:`pr_slug`), so #7 in two different repos never share a session, and a
    forced review collides with the auto monitor's session for the same PR."""
    from backend.ticket_ingestion.models import pr_slug

    return f"pr-{pr_slug(pr)}"


def skip_reasons(
    pr, processed: set, login: str, gh, now: datetime | None = None
) -> list[str]:
    """Why the auto monitor would skip ``pr`` right now (empty = eligible).

    Mirrors the exact filters in ``PRMonitor.scan`` so the UI explains the
    monitor's behavior instead of guessing at it.
    """
    reasons: list[str] = []
    if (pr.repo, pr.number) in processed:
        reasons.append("already reviewed (recorded in the processed ledger)")
    if login and pr.author != login:
        reasons.append(f"authored by {pr.author}, not you ({login})")
    now = now or datetime.now(timezone.utc)
    age_min = (now - pr.created_at).total_seconds() / 60.0
    # This repo's own grace period, which its card may have overridden.
    min_age = gh.min_age_for(pr.repo)
    if age_min < min_age:
        left = max(1, round(min_age - age_min))
        reasons.append(f"in the min-age grace period ({left} min left)")
    # The monitor asks GitHub only for PRs into this repo's base branch, so a PR
    # into another one is invisible to auto review. The panel lists every open
    # PR, which means it has to say so — otherwise a PR sitting there with no
    # chips reads as "queued" when the monitor will never see it.
    base = gh.base_branch_for(pr.repo)
    if base and pr.base_ref != base:
        reasons.append(f"targets {pr.base_ref}, not the watched base ({base})")
    return reasons


async def list_open_prs() -> dict:
    """Every non-draft open PR on the watched repos, annotated for the UI."""
    from backend.ticket_ingestion.pr_monitor import PRMonitor
    from backend.ticket_ingestion.state import load_processed_prs

    cfg = _load_config()
    gh = cfg.github
    if gh is None or not gh.repo_list():
        return {"repos": [], "enabled": False, "login": "", "prs": []}

    monitor = PRMonitor(gh)
    login, login_error = "", ""
    try:
        login = await monitor._authenticated_user_login()
    except Exception as err:  # noqa: BLE001 — no token / network; still list PRs
        login_error = str(err)
        _logger.warning("Could not resolve GitHub login: %s", err)

    prs = []
    for repo in gh.repo_list():
        # all_bases: the panel lists every open PR and explains the skips in
        # chips, so a base-branch filter must not silently remove rows — it
        # becomes a reason instead (and stays force-reviewable).
        prs.extend(await monitor._list_prs(repo, all_bases=True))

    from backend.ticket_ingestion.models import pr_slug

    processed = load_processed_prs(_REPO_ROOT)
    now = datetime.now(timezone.utc)
    # The dir PR workspaces are provisioned under. Optional by design: a config
    # that doesn't resolve one costs the rows their reopen probe, which is a
    # missing button — never a missing PR.
    workspace_dir = str(getattr(cfg, "workspace_dir", "") or "")
    out = []
    for pr in sorted(prs, key=lambda p: p.created_at, reverse=True):
        reasons = skip_reasons(pr, processed, login, gh, now)
        out.append(
            {
                "repo": pr.repo,
                "number": pr.number,
                "title": pr.title,
                "url": pr.url,
                "author": pr.author,
                "base_ref": pr.base_ref,
                "head_ref": pr.head_ref,
                "created_at": pr.created_at.isoformat(),
                "session": session_title(pr),
                # Where a review of this PR keeps its clone. Derived exactly as
                # ``PRProvisioner`` derives it (and pure, unlike provisioning
                # itself), so the reopen probe in server.py can tell whether an
                # earlier review's workspace is still on this machine.
                "workspace_path": (
                    str(Path(workspace_dir) / f"pr-{pr_slug(pr)}")
                    if workspace_dir
                    else ""
                ),
                "eligible": not reasons,
                "reasons": reasons,
            }
        )
    return {
        "repos": gh.repo_list(),
        "enabled": gh.enabled,
        "login": login,
        "login_error": login_error,
        "prs": out,
    }


async def find_pr(repo: str, number: int):
    """The live GitHub record for one open PR (drafts excluded)."""
    from backend.ticket_ingestion.pr_monitor import PRMonitor

    cfg = _load_config()
    if cfg.github is None:
        raise LookupError("PR review is not configured — add a repository first")
    monitor = PRMonitor(cfg.github)
    # all_bases, matching the panel: forcing a review is exactly how you take a
    # PR the auto monitor's base filter skips.
    for pr in await monitor._list_prs(repo, all_bases=True):
        if pr.number == number:
            return pr
    raise LookupError(
        f"No open PR #{number} in {repo} — closed, merged or draft PRs "
        "can't be force-reviewed"
    )


async def prepare_review(pr) -> tuple[Path, str, int]:
    """Provision the PR's workspace and build its session prompt.

    Slow (git clone/fetch) — run off the request path. Returns the workspace
    directory, the prompt, and how many actionable comments it addresses.
    """
    from backend.ticket_ingestion.pr_comments import fetch_actionable_comments
    from backend.ticket_ingestion.pr_provisioner import PRProvisioner
    from backend.ticket_ingestion.pr_runner import build_consolidated_pr_prompt

    cfg = _load_config()
    comments = await fetch_actionable_comments(pr, cfg.github)
    workspace = await PRProvisioner(cfg).provision(pr, launch_cursor=False)
    if comments:
        prompt = build_consolidated_pr_prompt(pr, comments, workspace.directory)
    else:
        prompt = _self_review_prompt(pr, workspace.directory)
    return workspace.directory, prompt, len(comments)


def _self_review_prompt(pr, workspace_dir) -> str:
    """Prompt for a forced review with no actionable reviewer comments: review
    the diff itself instead of addressing feedback."""
    return "\n".join(
        [
            f"# PR #{pr.number}: {pr.title}",
            "",
            f"- PR URL: {pr.url}",
            f"- Branch (already checked out): `{pr.head_ref}` @ `{pr.head_sha}`",
            f"- Base branch: `{pr.base_ref}`",
            f"- Workspace: `{workspace_dir}`",
            f"- Author: {pr.author}",
            "",
            "## Your task",
            "",
            "This review was started manually and the PR has no unresolved "
            "actionable reviewer comments, so review the change itself:",
            "",
            f"1. Read the full diff of this branch against `{pr.base_ref}` "
            f"(`git diff {pr.base_ref}...HEAD`).",
            "2. Look for real defects — correctness bugs, missing edge cases, "
            "broken error handling, security issues, missing test coverage.",
            "3. Fix what you find directly in this workspace. Leave the changes "
            "**unstaged** — do NOT run `git add`, `git commit`, or `git push`; "
            "the human reviews them in their editor and decides what to commit.",
            "4. Finish with a concise summary of every issue found, noting which "
            "you fixed and which need a human decision.",
        ]
    )


def record_reviewed(pr) -> None:
    """Mark ``pr`` processed so auto review doesn't run it a second time."""
    from backend.ticket_ingestion.models import ProcessedPR
    from backend.ticket_ingestion.state import (
        load_processed_prs,
        record_processed_pr,
    )

    if (pr.repo, pr.number) in load_processed_prs(_REPO_ROOT):
        return
    record_processed_pr(
        _REPO_ROOT,
        ProcessedPR(
            number=pr.number,
            head_sha=pr.head_sha,
            processed_at=datetime.now(timezone.utc),
            repo=pr.repo,
        ),
    )
