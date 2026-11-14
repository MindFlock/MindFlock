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

It also backs "Run the PR review" (:func:`run_pr_review`, ``POST
/api/pr-review``, MCP ``run_pr_review``): the team's saved review route
(``flights.pr_review_plan``) flown by an open PR's own session — started on
it, merged into the route it already flies, or on a session made for the PR.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.providers import effort as _provider_effort
from backend.web.core import pipeline_root
from backend.web.core.ticket_start import LaunchError, launched_elsewhere

__all__ = [
    "LaunchError",
    "configured_plan",
    "find_pr_session",
    "launch",
    "parse_pr_url",
    "resolve_plan",
    "run_pr_review",
]

_logger = logging.getLogger(__name__)


def _resolve_repo_root() -> Path:
    """Where the pipeline keeps state.json / workspaces/ — see
    :func:`backend.web.core.pipeline_root.resolve_repo_root`."""
    return pipeline_root.resolve_repo_root(__file__)


# The pipeline runs with cwd = repo root and keeps state.json / workspaces/
# there; this server's cwd is not guaranteed to match, so every relative path
# is anchored here explicitly.
_REPO_ROOT = _resolve_repo_root()


def _load_config():
    """The pipeline's config, ``workspace_dir`` anchored at :data:`_REPO_ROOT`
    (:func:`backend.web.core.pipeline_root.load_config`)."""
    return pipeline_root.load_config(_REPO_ROOT)


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import) —
    :func:`launch` registers the session in its live engine, and calling back
    through its namespace keeps ``monkeypatch.setattr(server, …)`` working."""
    from backend.web import server

    return server


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
        reasons.append("already handled before")
    if login and pr.author != login:
        reasons.append(f"opened by {pr.author}, not you ({login})")
    now = now or datetime.now(timezone.utc)
    age_min = (now - pr.created_at).total_seconds() / 60.0
    # This repo's own grace period, which its card may have overridden.
    min_age = gh.min_age_for(pr.repo)
    if age_min < min_age:
        left = max(1, round(min_age - age_min))
        reasons.append(f"too new — waits {left} more min")
    # The monitor asks GitHub only for PRs into this repo's base branch, so a PR
    # into another one is invisible to auto review. The panel lists every open
    # PR, which means it has to say so — otherwise a PR sitting there with no
    # chips reads as "queued" when the monitor will never see it.
    base = gh.base_branch_for(pr.repo)
    if base and pr.base_ref != base:
        reasons.append(f"into {pr.base_ref}, not {base}")
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

    processed = load_processed_prs(_REPO_ROOT)
    now = datetime.now(timezone.utc)
    # The dir PR workspaces are provisioned under. Optional by design: a config
    # that doesn't resolve one costs the rows their reopen probe, which is a
    # missing button — never a missing PR.
    workspace_dir = str(getattr(cfg, "workspace_dir", "") or "")
    try:
        workspaces = await asyncio.to_thread(
            review_workspace_paths, prs, cfg, workspace_dir
        )
    except Exception:  # noqa: BLE001 — a probe never costs the listing
        workspaces = {}
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
                # Where an earlier review of this PR left its workspace (see
                # review_workspace_paths), so the reopen probe in server.py can
                # tell whether it is still on this machine.
                "workspace_path": workspaces.get((pr.repo, pr.number), ""),
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


def review_workspace_paths(prs, cfg, workspace_dir: str) -> dict:
    """``{(repo, number): path}`` — where a review of each PR keeps its work.

    The worktree holding the PR's head branch in its repo's base clone (a
    review's normal workspace), else the PR's own clone ``pr-<slug>`` (the
    fallback, derived exactly as ``PRProvisioner`` derives it), else that
    clone's would-be path, which the probe then finds missing. Read-only: the
    base clones are only listed (one ``git worktree list`` per repo), never
    cloned or fetched. Blocking — run it off the event loop.
    """
    from backend import gitutil
    from backend.session.provisioned import (
        load_provision_settings,
        resolve_base_repo_dir,
    )
    from backend.ticket_ingestion.models import pr_slug

    held: dict = {}  # repo url -> {branch: worktree path}

    def _held_by(repo_url: str) -> dict:
        if repo_url in held:
            return held[repo_url]
        index: dict = {}
        try:
            settings = load_provision_settings(repo_url_override=repo_url or None)
            base = str(resolve_base_repo_dir(settings)) if settings else ""
            if base and os.path.isdir(os.path.join(base, ".git")):
                real_base = os.path.realpath(base)
                for wt in gitutil.worktree_list(base, timeout=15) or ():
                    ref = str(wt.get("branch") or "")
                    path = str(wt.get("path") or "")
                    if (
                        ref.startswith("refs/heads/")
                        and path
                        and os.path.realpath(path) != real_base
                    ):
                        index[ref[len("refs/heads/") :]] = path
        except Exception:  # noqa: BLE001
            index = {}
        held[repo_url] = index
        return index

    out: dict = {}
    for pr in prs:
        clone = str(Path(workspace_dir) / f"pr-{pr_slug(pr)}") if workspace_dir else ""
        path = ""
        if not (clone and os.path.isdir(os.path.join(clone, ".git"))):
            path = _held_by(review_repo_url(pr, cfg)).get(pr.head_ref or "", "")
        out[(pr.repo, pr.number)] = path or clone
    return out


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


@dataclass
class PreparedReview:
    """Everything :func:`launch` needs to start a review session on ``pr``.

    ``strategy`` is the engine workspace strategy: ``"worktree"`` on the fast
    path (the PR head is a branch of the repo's shared base clone and the
    engine adds the worktree at Start), ``"clone"`` on the fallback, where
    ``workspace_path`` is the per-PR clone the session adopts.
    """

    prompt: str
    n_comments: int
    strategy: str
    repo_url: str = ""
    workspace_path: str = ""
    base_branch: str = ""
    fallback_reason: str = ""


#: Stand-in for the workspace path in prompts built before the worktree exists
#: (its directory name is only chosen at Start); replaced by plain words.
_HERE = "<<mindflock-review-workspace>>"
_HERE_TEXT = "the current directory (this session's worktree of the repo)"


def review_repo_url(pr, cfg) -> str:
    """The URL a review of ``pr`` provisions from — its own repo, spelled with
    the configured transport (as :class:`PRProvisioner` clones it); ``""`` =
    the configured repository."""
    from backend.ticket_ingestion.clone_transport import (
        apply_transport,
        resolve_transport,
    )

    url = str(getattr(pr, "clone_url", "") or "")
    if not url:
        return ""
    try:
        return apply_transport(url, resolve_transport(cfg))
    except Exception:  # noqa: BLE001 — an odd transport setting: use it as given
        return url


def _is_live_worktree(path: str) -> bool:
    """Whether a live session works in ``path`` (the engine's own check)."""
    from backend.web.core.workspaces import _worktree_in_use_by_other

    return _worktree_in_use_by_other(path, "")


async def _provision(pr, cfg, repo_url: str) -> PreparedReview:
    """The workspace half of :func:`prepare_review` (``prompt`` left empty)."""
    from backend.ticket_ingestion.pr_provisioner import (
        PRProvisioner,
        prepare_pr_worktree,
    )
    from backend.web.core.ticket_start import workspace_mode

    reason = ""
    if workspace_mode() == "clone":
        reason = 'workspaces are configured mode = "clone"'
    else:
        try:
            wt = await asyncio.to_thread(
                prepare_pr_worktree, pr, repo_url=repo_url, is_live=_is_live_worktree
            )
            return PreparedReview(
                prompt="",
                n_comments=0,
                strategy="worktree",
                repo_url=repo_url,
                base_branch=wt.base_branch,
            )
        except Exception as err:  # noqa: BLE001 — the clone below is the backstop
            reason = str(err) or type(err).__name__
    _logger.info(
        "PR #%s: using its own clone instead of the base clone (%s)",
        pr.number,
        reason,
    )
    # Setup commands and cache seeds run in the session's own Setup, once.
    workspace = await PRProvisioner(cfg).provision(
        pr, launch_cursor=False, run_setup=False
    )
    return PreparedReview(
        prompt="",
        n_comments=0,
        strategy="clone",
        repo_url=repo_url,
        workspace_path=str(workspace.directory),
        base_branch=str(getattr(pr, "base_ref", "") or ""),
        fallback_reason=reason,
    )


async def prepare_review(pr) -> PreparedReview:
    """Ready the PR's workspace and build its session prompt.

    The workspace is a branch of the repo's shared base clone, which the
    session's worktree is added from at Start, the same as a ticket's: one
    incremental fetch instead of a clone. It falls back to the PR's own clone
    when the head branch is a live session's, no base clone can be made, or
    workspaces are configured ``mode = "clone"``. The reviewer comments are
    fetched at the same time. Run it off the request path.
    """
    from backend.ticket_ingestion.pr_comments import fetch_actionable_comments
    from backend.ticket_ingestion.pr_runner import build_consolidated_pr_prompt

    cfg = _load_config()
    repo_url = review_repo_url(pr, cfg)
    comments, prepared = await asyncio.gather(
        fetch_actionable_comments(pr, cfg.github),
        _provision(pr, cfg, repo_url),
        return_exceptions=True,
    )
    for result in (prepared, comments):
        if isinstance(result, BaseException):
            raise result
    where = prepared.workspace_path or _HERE
    if comments:
        prompt = build_consolidated_pr_prompt(pr, comments, where)
    else:
        prompt = _self_review_prompt(pr, where)
    prepared.prompt = prompt.replace("`%s`" % _HERE, _HERE_TEXT).replace(
        _HERE, _HERE_TEXT
    )
    prepared.n_comments = len(comments)
    return prepared


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
            # origin/<base>: the workspace always has the PR's base fetched
            # there, but a local branch of that name only when it is the
            # repo's own base.
            f"1. Read the full diff of this branch against `{pr.base_ref}` "
            f"(`git diff origin/{pr.base_ref}...HEAD`).",
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


async def launch(
    repo: str,
    number: int,
    *,
    program: str = "",
    depth: str = "",
    effort: str = "",
    plan: str = "",
) -> dict:
    """Start a review session for one open PR and return the 202 body.

    The body of ``POST /api/github/prs/review`` (which validates its payload
    and calls this), callable without HTTP: ``POST /api/flights/start`` with a
    ``pr`` subject calls it too. The workspace is provisioned and the session
    registered on a background task; a launch failure is reported as
    ``session.create_failed``.

    * ``program``: the CLI to run; ``""`` = this repo's review agent, then the
      app default (the route resolves the chain itself, with its own pick
      first).
    * ``depth`` / ``effort``: per-start overrides (``""`` = the repo's).
    * ``plan``: the flight plan to fly; ``""`` = ``server._intake_plan``.

    Raises :class:`LaunchError` (404 no such open PR, 409 a session for it
    already exists, 502 GitHub failed).
    """
    srv = _server()
    chosen = str(program or "")
    effort_override = str(effort or "")
    # The provisioning row goes up BEFORE the GitHub lookup below, which is the
    # last thing standing between the click and the sidebar showing anything.
    # The panel's cached list already carries the title this PR maps to, so no
    # slug re-derivation is involved; a cold cache just falls back to the
    # post-lookup registration further down.
    early = srv._cached_session_title(
        srv._OPEN_PRS_CACHE,
        "prs",
        lambda p: p.get("repo") == repo and p.get("number") == number,
    )
    if early:
        if early in srv.ENGINE.instances or srv._pending_has(early):
            raise LaunchError(
                409,
                {
                    "error": "session %s already exists — close it to re-review"
                    % early,
                    "title": early,
                },
            )
        srv._pending_add(early, "pr", repo=repo)

    try:
        pr = await find_pr(repo, number)
    except LookupError as err:
        srv._pending_drop(early)
        raise LaunchError(404, {"error": str(err)}) from None
    except Exception as err:  # noqa: BLE001
        srv._pending_drop(early)
        raise LaunchError(502, {"error": str(err)}) from None

    title = session_title(pr)
    if title != early:
        srv._pending_drop(early)  # stale cache entry — keep only the real title
        if title in srv.ENGINE.instances or srv._pending_has(title):
            raise LaunchError(
                409,
                {
                    "error": "session %s already exists — close it to re-review"
                    % title,
                    "title": title,
                },
            )
    # Re-registered with the branch now that it's known (add() keeps `since`).
    srv._pending_add(title, "pr", branch=pr.head_ref, repo=repo)
    repo_depth = srv._repo_intake_depth(repo, "prs")
    armed: list = []
    flight_tail = srv._arm_intake(
        title,
        depth or repo_depth,
        "pr",
        "%s#%s" % (repo, number),
        message=str(getattr(pr, "title", "") or ""),
        plan=srv._intake_plan(
            "pr", plan, depth, srv._repo_intake_plan(repo, "prs"), repo_depth
        ),
        subject={
            "kind": "pr",
            "source": repo,
            "id": str(number),
            "title": str(getattr(pr, "title", "") or ""),
            "url": str(getattr(pr, "url", "") or ""),
        },
        branch=str(getattr(pr, "head_ref", "") or ""),
        armed=armed,
    )

    async def _bg_review() -> None:
        # Ready the workspace first (a fetch of the PR head into the base
        # clone; the PR's own clone only as a fallback), then register the
        # session — same shape as the pipeline's
        # SessionRunner.run_pr, but against THIS server's engine so the session
        # shows up in the grid without a reload.
        inst = None
        try:
            prepared = await prepare_review(pr)
            prompt, n_comments = prepared.prompt, prepared.n_comments
            # The route's pick (its own, this repo's card, Intake → Pull
            # requests' Agent CLI), else the same chain without a pick.
            # Resolved first because this start's effort has to be translated
            # into THAT CLI's spelling.
            program = chosen or review_agent(repo) or srv.ENGINE.default_program()
            prompt += flight_tail
            prompt = _provider_effort.decorate_prompt(prompt, program, effort_override)
            # A fallback clone exists already: decorate against it. The fast
            # path's worktree is only added at Start, so key off the repo's
            # base clone, as a ticket start does.
            prompt = await asyncio.to_thread(
                lambda p=prompt: srv._red_zone_prompt(
                    p,
                    program,
                    (
                        [prepared.workspace_path]
                        if prepared.workspace_path
                        else srv._repo_url_workdirs(prepared.repo_url)
                    ),
                    None,
                )
            )
            inst = srv.session.NewInstance(
                srv.session.InstanceOptions(
                    title=title,
                    path=".",
                    program=program,
                    provisioned=True,
                    workspace_strategy=prepared.strategy,
                    # Exact: the engine uses an existing branch as named.
                    new_branch=pr.head_ref,
                    prompt=prompt,
                    provision_repo_url=prepared.repo_url,
                    workspace_path=prepared.workspace_path,
                    base_branch=prepared.base_branch,
                    launch_args=srv._start_launch_args(program, effort_override),
                )
            )
            inst.ExtraEnv = srv._ports.env_for(title)
            inst.SetStatus(srv.Loading)
            with srv.ENGINE.lock:
                srv.ENGINE.instances[title] = inst
            srv._seed_event_snapshot(title)
            srv._events.BUS.emit(
                "session.created",
                session=title,
                new="loading",
                data={"program": inst.Program, "provisioned": True, "pr": pr.number},
            )
            try:
                await asyncio.to_thread(inst.Start, True)
                srv.ENGINE.save()
            except Exception:
                # By identity: this task may be the loser of a re-start, and
                # popping by name would delete the LIVE session's record.
                srv._drop_failed_start(title, inst)
                raise
            # Ledger the PR so auto review doesn't run it a second time.
            record_reviewed(pr)
            if srv.log.InfoLog is not None:
                srv.log.InfoLog.Printf(
                    "forced PR review %s live (%d comments)", title, n_comments
                )
        except Exception as err:  # noqa: BLE001
            if launched_elsewhere(srv, title, inst, err):
                return
            if srv.log.ErrorLog is not None:
                srv.log.ErrorLog.Printf("forced PR review %s failed: %v", title, err)
            srv._events.BUS.emit(
                "session.create_failed", session=title, data={"error": str(err)}
            )
            srv._cancel_intake_flight(
                title,
                "the session could not be created: %s" % err,
                armed[0] if armed else "",
            )
        finally:
            srv._pending_drop(title)

    srv._register_task(_bg_review())
    return {"started": True, "title": title}


# --------------------------------------------------------------------------- #
# "Run the PR review" on an open PR (docs/flights.md "Run the PR review on an
# open PR"): the team's saved review route, flown by the PR's own session.
# --------------------------------------------------------------------------- #
_PR_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/\s?#]+)/([^/\s?#]+)/pulls?/(\d+)(?:[/?#].*)?$",
    re.I,
)


def _flights_mod():
    from backend.web.core import flights

    return flights


def _driver_mod():
    from backend.web.core import flight_driver

    return flight_driver


def _err(code: str, status: int, detail: str):
    return _flights_mod().FlightError(code, status, detail)


def parse_pr_url(url: str) -> tuple[str, int]:
    """``https://github.com/<owner>/<repo>/pull/<n>`` → ``("owner/repo", n)``.
    400 ``bad_pr_url`` for anything else."""
    m = _PR_URL_RE.match(str(url or "").strip())
    if m is None:
        raise _err(
            "bad_pr_url",
            400,
            "not a GitHub pull request URL (https://github.com/<owner>/<repo>"
            "/pull/<number>): %s" % str(url or "")[:200],
        )
    repo = m.group(2)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return "%s/%s" % (m.group(1), repo), int(m.group(3))


def _same_pr(a: str, b: str) -> bool:
    """Whether two PR URLs name the same PR (case, a trailing slash and a
    ``/files`` tail aside)."""
    if not a or not b:
        return False
    try:
        (ra, na), (rb, nb) = parse_pr_url(a), parse_pr_url(b)
    except Exception:  # noqa: BLE001 — a row's url may be anything
        return False
    return na == nb and ra.lower() == rb.lower()


def configured_plan() -> str:
    """``flights.pr_review_plan`` (a plan id or name), ``""`` = none. A local
    import, as every settings reader in the web layer does."""
    try:
        from backend.config.settings import load_settings

        return str(load_settings().flights.pr_review_plan or "").strip()
    except Exception:  # noqa: BLE001 — an unreadable store is "not configured"
        return ""


def resolve_plan(ref: str = "") -> dict:
    """The review route: ``ref`` (a saved plan's id, else its name, case
    aside), else the ``flights.pr_review_plan`` setting. 400 ``no_plan`` when
    neither names one; 404 ``unknown_plan`` when it is not on the shelf."""
    F = _flights_mod()
    ref = str(ref or "").strip() or configured_plan()
    if not ref:
        raise _err(
            "no_plan",
            400,
            "no PR review route: pick one in Settings (flights.pr_review_plan) "
            "or pass plan",
        )
    plan = F.get_plan(ref)
    if plan is None:
        low = ref.lower()
        plan = next(
            (
                p
                for p in F.list_plans()
                if str(p.get("name") or "").strip().lower() == low
            ),
            None,
        )
    if plan is None or not plan.get("steps"):
        raise _err("unknown_plan", 404, "no saved route %s" % ref)
    return plan


def first_stop(plan: dict) -> str:
    """Where the review route starts on a session that has none: its first
    stop — past any leading Agent stop with no prompt of its own (that one
    stands for "the work", which is already on the PR; flown, it would only
    wait for a prompt nobody is going to type). An Agent stop WITH a prompt
    is where it starts: its prompt is sent."""
    for s in plan["steps"]:
        if (
            s["kind"] == "agent"
            and not str((s.get("config") or {}).get("prompt") or "").strip()
        ):
            continue
        return s["id"]
    raise _err(
        "bad_plan",
        400,
        "the route %s has only empty Agent stops: nothing to run on an open PR"
        % (plan.get("name") or plan.get("id")),
    )


def _row_of(title: str) -> dict:
    from backend.web.core import events as _events

    for r in _events.sessions_snapshot():
        if isinstance(r, dict) and r.get("title") == title:
            return r
    return {}


def _repo_slug(title: str) -> str:
    """``owner/repo`` behind the session's origin (lower-cased), ``""`` when
    unknown (blocking: runs git)."""
    from backend.web.core import github_pr as _gh

    root = _driver_mod()._repo_root(title)
    ref = _gh.repo_ref(root) if root else None
    return ref.slug.lower() if ref is not None else ""


async def pr_head(slug: str, number: int) -> str:
    """The PR's head branch, ``""`` when it can't be read: the REST API with
    the app's token, else the Intake monitor's own lookup (which needs
    ``[github]`` configured)."""
    from backend.web.core import github_pr as _gh

    token = await _gh.api_token()
    if token:
        status, data = await _gh._request(
            "GET", "/repos/%s/pulls/%d" % (slug, number), token=token
        )
        if status == 200 and isinstance(data, dict):
            head = data.get("head") if isinstance(data.get("head"), dict) else {}
            ref = str(head.get("ref") or "")
            if ref:
                return ref
    try:
        pr = await find_pr(slug, number)
    except Exception:  # noqa: BLE001 — not configured / not open / offline
        return ""
    return str(getattr(pr, "head_ref", "") or "")


async def find_pr_session(slug: str, number: int, url: str = "") -> str:
    """The live session already working on PR ``slug#number``, ``""`` when
    none: one whose row shows that PR, else Intake's own session for it
    (``pr-<repo>-<n>``), else one whose branch is the PR's head in that
    repo. Never a second session on the PR's branch."""
    srv = _server()
    live = dict(srv.ENGINE.instances)
    url = url or "https://github.com/%s/pull/%d" % (slug, number)
    from backend.web.core import events as _events

    for r in _events.sessions_snapshot():
        t = r.get("title") if isinstance(r, dict) else None
        if t in live and _same_pr(str(r.get("pr_url") or ""), url):
            return t
    want = slug.lower()
    from backend.ticket_ingestion.models import PullRequest, pr_slug

    intake = "pr-%s" % pr_slug(
        PullRequest(
            number=number,
            head_ref="",
            head_sha="",
            base_ref="",
            title="",
            url=url,
            author="",
            created_at=datetime.now(timezone.utc),
            repo=slug,
        )
    )
    if intake in live and await asyncio.to_thread(_repo_slug, intake) in ("", want):
        return intake
    head = await pr_head(slug, number)
    if not head:
        return ""
    for t, inst in sorted(live.items()):
        if str(getattr(inst, "Branch", "") or "") != head:
            continue
        if await asyncio.to_thread(_repo_slug, t) == want:
            return t
    return ""


def _refuse_stopped(title: str, inst) -> None:
    """409 when the session can't take a route now: paused (its worktree is
    put away) or held offline after a crash (nothing may start its agent
    but a person)."""
    from backend.web.core import held as _held

    if _held.is_held(title):
        raise _err(
            "held",
            409,
            "%s is held offline since its agent died — start it first" % title,
        )
    paused = getattr(inst, "Paused", None)
    try:
        is_paused = bool(paused()) if callable(paused) else False
    except Exception:  # noqa: BLE001
        is_paused = False
    if is_paused:
        raise _err("paused", 409, "%s is paused — resume it first" % title)


def _pr_subject(slug: str, number: int, url: str, title: str = "") -> dict:
    return {
        "kind": "pr",
        "source": slug,
        "id": str(number),
        "title": title,
        "url": url,
    }


def _stops(steps) -> list:
    return [
        {"id": s["id"], "kind": s["kind"], "title": s.get("title") or s["kind"]}
        for s in steps
    ]


def _result(mode: str, title: str, flight, steps) -> dict:
    return {
        "mode": mode,
        "session": title,
        "flight": str((flight or {}).get("id") or ""),
        "stops": _stops(steps),
    }


async def _run_on_session(title: str, plan: dict, by: str, subject) -> dict:
    """Start the review route on ``title`` (no route yet) or merge it into
    the route it flies (:func:`flights.op_merge_plan`)."""
    F = _flights_mod()
    D = _driver_mod()
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        raise _err("unknown_session", 404, "no session %s" % title)
    _refuse_stopped(title, inst)
    flight = F.for_session(title)
    if flight is None:
        if subject is None:
            pr_url = str(_row_of(title).get("pr_url") or "")
            try:
                slug, number = parse_pr_url(pr_url)
                subject = _pr_subject(slug, number, pr_url)
            except F.FlightError:
                subject = {"kind": "manual", "title": plan.get("name") or ""}
        f = await D.start(
            {
                "plan_id": plan["id"],
                "title": title,
                "start_at": first_stop(plan),
                "by": by,
                "subject": subject,
            }
        )
        return _result("started", title, f, f["steps"][f["cursor"] :])
    added: list = []
    after = await asyncio.to_thread(
        D._changed,
        flight["id"],
        lambda f: F.op_merge_plan(f, plan, added=added),
    )
    return _result("inserted", title, after, added)


async def run_pr_review(
    session_title: str | None = None,
    pr_url: str | None = None,
    plan: str | None = None,
    by: str = "user",
) -> dict:
    """Run the team's PR review route on an open PR's session → ``{"mode":
    "started" | "inserted" | "created", "session", "flight", "stops":
    [{id, kind, title}]}`` (``POST /api/pr-review``, MCP ``run_pr_review``).

    * ``plan``: a saved route's id or name; ``None``/``""`` = the
      ``flights.pr_review_plan`` setting (400 ``no_plan`` when neither).
    * ``session_title``: the session to run it on (404 ``unknown_session``;
      409 ``paused`` / ``held``). Without it, ``pr_url`` finds the session
      (:func:`find_pr_session`: the one showing that PR, Intake's own, or
      one on the PR's head branch in that repo).

    One route per session, so what happens depends on the session:

    * **No route** → ``started``: the plan takes off on it, the work already
      done — it starts at the plan's first stop (:func:`first_stop`: past a
      leading Agent stop with no prompt, so nothing waits for a prompt).
      ``stops`` = the stops ahead.
    * **Mid-route** → ``inserted``: the plan's stops are merged in after the
      current stop (:func:`flights.op_merge_plan`): fresh ids, a stop the
      route already ran or has ahead (same kind and config) left out, and
      in front of a ``reviews`` / ``ship`` / Land stop already ahead.
      ``stops`` = the stops added (``[]``: all were already there).
    * **Only a PR URL, no session on it** → ``created``: a session for the
      PR is made as Intake's Pull requests tab makes one (:func:`launch`,
      provisioned on the PR's head branch) with this plan armed; it comes
      up in the background. ``stops`` = the route's stops.

    Fixes are made by the session's own agent in its own checkout; no second
    session is ever put on the PR's branch. Raises
    :class:`~backend.web.core.flights.FlightError` (400 / 404 / 409) and,
    on the create path, :class:`LaunchError` (404 / 409 / 502)."""
    F = _flights_mod()
    route = resolve_plan(plan or "")
    title = str(session_title or "").strip()
    url = str(pr_url or "").strip()
    if not title and not url:
        raise _err("needs_target", 400, "name the session or the PR's URL")
    subject = None
    if url:
        slug, number = parse_pr_url(url)
        subject = _pr_subject(slug, number, url)
    if title:
        return await _run_on_session(title, route, by, subject)
    found = await find_pr_session(slug, number, url)
    if found:
        return await _run_on_session(found, route, by, subject)
    out = await launch(slug, number, plan=route["id"])
    title = str(out.get("title") or "")
    flight = F.for_session(title)
    return _result("created", title, flight, (flight or route)["steps"])
