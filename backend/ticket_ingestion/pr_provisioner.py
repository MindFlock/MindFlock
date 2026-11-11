"""Provision a workspace for a PR: clone (or fetch) the configured repo and check out the PR head branch."""

import asyncio
import logging
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from backend.ticket_ingestion._subprocess import run_capture
from backend.ticket_ingestion.clone_transport import (
    apply_transport,
    clone_failure_hint,
    resolve_transport,
    run_network_git,
)
from backend.ticket_ingestion.config import PipelineConfig
from backend.ticket_ingestion.models import (
    ProvisionedPRWorkspace,
    PullRequest,
    pr_slug,
)
from backend.session.provisioned import record_base_branch
from backend.workspace_setup import (
    pin_cache_env,
    resolve_setup_commands,
    run_setup_commands_async,
    seed_caches,
)

_logger = logging.getLogger(__name__)

# Wall-clock caps so a hung git subprocess (e.g. a clone/fetch over a stalled
# TCP connection) fails this PR instead of wedging the PR loop forever.
_NETWORK_GIT_TIMEOUT = 600.0  # clone / fetch
_LOCAL_GIT_TIMEOUT = 120.0  # checkout / reset


@contextmanager
def _step(pr_number: int, label: str):
    _logger.info("PR #%d: %s starting", pr_number, label)
    start = time.monotonic()
    try:
        yield
    finally:
        _logger.info(
            "PR #%d: %s finished in %.1fs", pr_number, label, time.monotonic() - start
        )


class PRProvisioningError(Exception):
    """Raised when cloning, fetching or checking out a PR's workspace fails."""


class PRProvisioner:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    async def provision(
        self, pr: PullRequest, launch_cursor: bool = True, run_setup: bool = True
    ) -> ProvisionedPRWorkspace:
        """Clone (or refresh) ``pr``'s own workspace at its head.

        ``run_setup=False`` stops after the git work: the caller hands the
        directory to a provisioned engine session, whose ``Setup`` runs the
        setup commands and cache seeds itself (once, not twice).
        """
        # Absolute path: the workspace is handed to MindFlock, whose tmux
        # session runs the launcher via `-c <dir>` and execs the workspace
        # launcher script (.mindflock_launch.sh). tmux resolves a relative path against the tmux *server's*
        # cwd (not ours), so a relative dir makes the launcher unfindable — the
        # pane exits instantly and the session-start times out. An absolute path
        # also lets the web UI open the workspace in Cursor correctly.
        directory = (Path(self.config.workspace_dir) / f"pr-{pr_slug(pr)}").resolve()
        _logger.info(
            "Provisioning PR workspace #%d at %s (head=%s @ %s)",
            pr.number,
            directory,
            pr.head_ref,
            pr.head_sha[:8],
        )

        if directory.exists() and (directory / ".git").exists():
            try:
                with _step(pr.number, "git refresh"):
                    await self._refresh(pr, directory)
            except PRProvisioningError as e:
                # A prior run may have left a corrupt/partial clone (e.g. an
                # interrupted or concurrent clone). Don't fail forever on it —
                # blow it away and re-clone from scratch.
                _logger.warning(
                    "PR #%d: refresh failed (%s); re-cloning from scratch",
                    pr.number,
                    e,
                )
                shutil.rmtree(directory, ignore_errors=True)
                with _step(pr.number, "git clone"):
                    await self._clone(pr, directory)
        else:
            if directory.exists():
                shutil.rmtree(directory)
            with _step(pr.number, "git clone"):
                await self._clone(pr, directory)

        stale_lock = directory / ".pr-edit-lock"
        if stale_lock.exists():
            stale_lock.unlink()
        # Diff base for the session's Setup when the PR is not into the repo's
        # configured base branch.
        record_base_branch(directory, pr.head_ref, pr.base_ref)
        if not run_setup:
            return ProvisionedPRWorkspace(
                directory=directory, head_ref=pr.head_ref, head_sha=pr.head_sha
            )
        with _step(pr.number, "workspace setup"):
            await run_setup_commands_async(
                resolve_setup_commands(self.config.setup_commands, directory),
                directory,
                log_prefix=f"PR #{pr.number}",
            )
        pin_cache_env(directory, self.config.caches)
        with _step(pr.number, "seed caches"):
            seed_caches(self.config.caches, directory, log_prefix=f"PR #{pr.number}")
        if launch_cursor:
            with _step(pr.number, "launch cursor"):
                await self._launch_cursor(pr, directory)

        return ProvisionedPRWorkspace(
            directory=directory, head_ref=pr.head_ref, head_sha=pr.head_sha
        )

    async def _launch_cursor(self, pr: PullRequest, directory: Path) -> None:
        from backend.config import ide as _ide
        from backend.web.core import ide_launch as _ide_launch

        _logger.info(
            "Opening %s for PR #%d at %s", _ide.ide_name(), pr.number, directory
        )
        try:
            # launch_ide is sync (fire-and-forget Popen); run it off-loop.
            await asyncio.to_thread(_ide_launch.launch_ide, str(directory))
        except Exception as err:  # noqa: BLE001
            _logger.warning(
                "%s launch failed for PR #%d (continuing): %s",
                _ide.ide_name(),
                pr.number,
                err,
            )

    async def _clone(self, pr: PullRequest, directory: Path) -> None:
        directory.parent.mkdir(parents=True, exist_ok=True)
        # Clone the PR's own repo (multi-repo review). The monitor already
        # picked the transport when it recorded clone_url; apply_transport only
        # re-spells it when the user explicitly configured ssh/https (and leaves
        # any other spelling — including a local path — exactly as given).
        clone_url = apply_transport(pr.clone_url, resolve_transport(self.config))
        # Blobless partial clone (matches the ticket path's canonical clone in
        # session/provisioned.py): fetches commit/tree objects now and blobs on
        # demand, which is dramatically faster than a full clone of a large repo
        # — that plain `git clone` was the bulk of PR provisioning time. `--no-tags`
        # skips the tag refs we never use. Full history is preserved so diffs
        # against the base branch still work.
        rc, _, stderr = await _run_network(
            "git",
            "clone",
            "--filter=blob:none",
            "--no-tags",
            clone_url,
            str(directory),
            timeout=_NETWORK_GIT_TIMEOUT,
        )
        if rc != 0:
            # Fallback: a server that doesn't support partial clone (or a
            # transient partial-clone failure). Retry with a plain full clone.
            _logger.warning(
                "PR #%d: blobless clone failed (%s); retrying with a full clone",
                pr.number,
                stderr.decode(errors="replace").strip(),
            )
            shutil.rmtree(directory, ignore_errors=True)
            rc, _, stderr = await _run_network(
                "git",
                "clone",
                clone_url,
                str(directory),
                timeout=_NETWORK_GIT_TIMEOUT,
            )
        if rc != 0:
            raise PRProvisioningError(
                f"git clone failed for PR #{pr.number}: "
                f"{stderr.decode(errors='replace').strip()} "
                f"[{clone_failure_hint(clone_url)}]"
            )
        await self._checkout_pr_head(pr, directory)

    async def _refresh(self, pr: PullRequest, directory: Path) -> None:
        await self._checkout_pr_head(pr, directory)

    async def _checkout_pr_head(self, pr: PullRequest, directory: Path) -> None:
        # Fetch the PR head via refs/pull/<n>/head, which GitHub exposes for both
        # same-repo and fork PRs. This avoids depending on origin/<head_ref>, which
        # does not exist when the branch lives on a fork.
        rc, _, stderr = await _run_network(
            "git",
            "fetch",
            "origin",
            f"pull/{pr.number}/head",
            cwd=str(directory),
            timeout=_NETWORK_GIT_TIMEOUT,
        )
        if rc != 0:
            raise PRProvisioningError(
                f"git fetch pull/{pr.number}/head failed for PR #{pr.number}: "
                f"{stderr.decode(errors='replace').strip()} "
                f"[{clone_failure_hint(pr.clone_url)}]"
            )
        # Pin the local branch to the exact head SHA we recorded. Fall back to the
        # fetched tip (FETCH_HEAD) if the PR was force-pushed since we polled and the
        # SHA is no longer reachable.
        target = pr.head_sha
        rc, _, stderr = await _run(
            "git", "checkout", "-B", pr.head_ref, target, cwd=str(directory)
        )
        if rc != 0:
            _logger.warning(
                "PR #%d: head SHA %s not reachable (%s); falling back to FETCH_HEAD",
                pr.number,
                pr.head_sha[:8],
                stderr.decode(errors="replace").strip(),
            )
            target = "FETCH_HEAD"
            rc, _, stderr = await _run(
                "git", "checkout", "-B", pr.head_ref, target, cwd=str(directory)
            )
            if rc != 0:
                raise PRProvisioningError(
                    f"git checkout -B {pr.head_ref} failed for PR #{pr.number}: "
                    f"{stderr.decode(errors='replace').strip()}"
                )
        # Discard any leftover working-tree changes from a prior provision so the
        # workspace is pristine at the recorded head.
        rc, _, stderr = await _run("git", "reset", "--hard", target, cwd=str(directory))
        if rc != 0:
            raise PRProvisioningError(
                f"git reset --hard {target} failed for PR #{pr.number}: "
                f"{stderr.decode(errors='replace').strip()}"
            )


async def _run(
    *args: str, cwd: str | None = None, timeout: float = _LOCAL_GIT_TIMEOUT
) -> tuple[int, bytes, bytes]:
    # The rc-124 timeout convention lives in the shared helper so it can't
    # drift from the provisioner / cache-refresher copies.
    return await run_capture(*args, cwd=cwd, timeout=timeout)


async def _run_network(
    *args: str, cwd: str | None = None, timeout: float = _NETWORK_GIT_TIMEOUT
) -> tuple[int, bytes, bytes]:
    # Same contract as _run, for the git calls that dial out (clone / fetch):
    # credential prompts disabled and stdin closed so a missing SSH key or PAT
    # errors in seconds instead of hanging until the wall-clock cap.
    return await run_network_git(*args, cwd=cwd, timeout=timeout)


# --------------------------------------------------------------------------- #
# Fast path: the PR head as a worktree of the repo's shared base clone
# --------------------------------------------------------------------------- #
#: Where a PR's head is fetched to inside the base clone. A private namespace:
#: never a branch (so nothing pushes it) and never a remote-tracking ref (so no
#: `git fetch` prunes it).
PR_REF_PREFIX = "refs/mindflock/pr/"


class PRWorktreeUnavailable(Exception):
    """The base-clone fast path can't serve this PR; the reason is the message.

    The caller falls back to the per-PR clone (:class:`PRProvisioner`).
    """


@dataclass
class PRWorktree:
    """What :func:`prepare_pr_worktree` left ready for a worktree session."""

    base_repo: str
    branch: str  # the PR's head_ref, spelled exactly
    head_sha: str  # what the branch now points at
    base_branch: str  # the PR's base_ref ("" = the repo's configured base)
    reclaimed: str = ""  # a leftover worktree freed for this run, if any


def pr_head_ref(number: int) -> str:
    """The base-clone ref a PR's head is fetched to."""
    return f"{PR_REF_PREFIX}{number}"


def _git(base: str, *args: str, timeout: float = _LOCAL_GIT_TIMEOUT):
    from backend.session import provisioned as _prov

    return _prov._run("git", "-C", base, *args, timeout=timeout)


def _out(cp) -> str:
    return (cp.stdout or b"").decode("utf-8", "replace").strip()


def prepare_pr_worktree(
    pr: PullRequest,
    *,
    repo_url: str = "",
    is_live: Callable[[str], bool] | None = None,
) -> PRWorktree:
    """Make ``pr``'s head a local branch of the repo's shared base clone.

    The ticket path's workspace, for a PR: the base clone
    (``workspaces/_base_<repo>``) already holds the repo's history, so one
    incremental ``git fetch origin pull/<n>/head`` brings the PR in (a fork PR
    too — GitHub serves ``refs/pull/<n>/head`` for both) and the session then
    gets an ordinary ``git worktree add`` of it. The old per-PR full clone
    downloaded every file of the repo on each PR's first review.

    Steps: resolve the provisioning settings for ``repo_url`` (the PR's clone
    URL; ``""`` = the configured repo) exactly as the engine will at Start;
    clone the base only when there is none yet (an existing one is refreshed by
    the engine's own ``ensure_base_repo`` at Start — doing it here too would
    fetch the base branch twice); fetch the PR head to
    ``refs/mindflock/pr/<n>`` and the PR's base branch to
    ``origin/<base_ref>`` (the diff base) in ONE fetch; then point a local
    branch named exactly ``pr.head_ref`` at the head.

    A worktree already holding ``head_ref``: a LIVE session's (``is_live``)
    or the base clone's own checkout makes this raise. A leftover one (a
    closed session's kept worktree) is freed: removed when it holds nothing
    beyond the PR's head, otherwise detached in place so its uncommitted
    changes and commits stay exactly where they are. A local ``head_ref``
    branch with commits that are in neither the PR's new head nor the head an
    earlier review fetched (a paused session's branch, whose worktree is gone
    while it sleeps) is never moved: that raises too.

    Blocking (git subprocesses) — run it off the event loop. Raises
    :class:`PRWorktreeUnavailable` whenever the caller should fall back to the
    per-PR clone instead.
    """
    from backend.session import provisioned as _prov

    branch = pr.head_ref or ""
    if not branch or branch.startswith("-"):
        raise PRWorktreeUnavailable(f"PR #{pr.number} has no usable head branch")
    try:
        settings = _prov.load_provision_settings(repo_url_override=repo_url or None)
    except Exception as err:  # noqa: BLE001
        raise PRWorktreeUnavailable(f"no provisioning settings: {err}") from err
    if settings is None:
        raise PRWorktreeUnavailable("no repository is configured to provision from")

    base_dir = _prov.resolve_base_repo_dir(settings)
    if not (base_dir / ".git").is_dir():
        try:
            with _step(pr.number, "base clone (one-time per repo)"):
                _prov.ensure_base_repo(settings)
        except Exception as err:  # noqa: BLE001
            raise PRWorktreeUnavailable(f"no base clone: {err}") from err
    base = str(base_dir)

    # Two repos with one name share a base-clone dirname; never fetch a PR
    # into the wrong repo's clone.
    want = repo_url or pr.clone_url or settings.repo_url
    origin = _prov._git_origin_url(base)
    if want and origin and not _prov._same_repo_url(origin, want):
        raise PRWorktreeUnavailable(f"base clone {base} tracks {origin}, not {want}")

    if (
        _git(base, "check-ref-format", "--branch", branch).returncode != 0
        or branch == "HEAD"
    ):
        raise PRWorktreeUnavailable(f"{branch!r} is not a valid local branch name")

    held = _prov.worktree_holding_branch(base, branch)
    if held and os.path.realpath(held) == os.path.realpath(base):
        raise PRWorktreeUnavailable(
            f"{branch} is the base clone's own checkout (the repo's base branch)"
        )
    if held and is_live is not None and is_live(held):
        raise PRWorktreeUnavailable(
            f"{branch} is checked out by a live session at {held}"
        )

    ref = pr_head_ref(pr.number)
    # The head an earlier review fetched, before this fetch replaces it: a
    # branch tip within it holds only PR history even after a force-push.
    previous = _out(_git(base, "rev-parse", "--verify", "--quiet", ref))
    pull = f"+refs/pull/{pr.number}/head:{ref}"
    refspecs = [pull]
    if pr.base_ref and not pr.base_ref.startswith("-"):
        refspecs.append(f"+refs/heads/{pr.base_ref}:refs/remotes/origin/{pr.base_ref}")
    with _step(pr.number, "fetch PR head into the base clone"):
        cp = _git(
            base,
            "fetch",
            "--no-tags",
            "origin",
            *refspecs,
            timeout=_NETWORK_GIT_TIMEOUT,
        )
        if cp.returncode != 0 and len(refspecs) > 1:
            # The base branch may be gone or renamed; the head alone still works.
            cp = _git(
                base, "fetch", "--no-tags", "origin", pull, timeout=_NETWORK_GIT_TIMEOUT
            )
    if cp.returncode != 0:
        raise PRWorktreeUnavailable(
            f"git fetch pull/{pr.number}/head failed: {_out(cp)[-500:]} "
            f"[{clone_failure_hint(origin or want)}]"
        )

    # The recorded head SHA when it is here (what the prompt names), else the
    # fetched tip — the PR was force-pushed since it was listed.
    target = _out(_git(base, "rev-parse", "--verify", "--quiet", ref + "^{commit}"))
    if pr.head_sha and not pr.head_sha.startswith("-"):
        recorded = _git(
            base, "rev-parse", "--verify", "--quiet", pr.head_sha + "^{commit}"
        )
        if recorded.returncode == 0 and _out(recorded):
            target = _out(recorded)
    if not target:
        raise PRWorktreeUnavailable(f"pull/{pr.number}/head did not resolve")

    known = [target] + ([previous] if previous else [])

    # Never move a branch off commits of its own: a paused session's branch
    # (its worktree is removed while paused) or a ticket's unpushed work that
    # shares the PR's branch name. Those stay put and the review falls back.
    old = _out(_git(base, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch))
    if old and not _within(base, old, known):
        raise PRWorktreeUnavailable(
            f"local branch {branch} has commits that are not in the PR"
        )

    reclaimed = ""
    if held:
        reclaimed = _free_leftover_worktree(base, held, branch, known)

    if old and old != target:
        _logger.info(
            "PR #%d: moving %s from %s to %s", pr.number, branch, old[:8], target[:8]
        )
    cp = _git(base, "branch", "-f", branch, target)
    if cp.returncode != 0:
        raise PRWorktreeUnavailable(f"git branch -f {branch} failed: {_out(cp)}")
    _prov.record_base_branch(base, branch, pr.base_ref)
    return PRWorktree(
        base_repo=base,
        branch=branch,
        head_sha=target,
        base_branch=pr.base_ref or "",
        reclaimed=reclaimed,
    )


def _within(repo: str, commit: str, heads: list) -> bool:
    """Whether ``commit`` is one of ``heads`` or an ancestor of one."""
    return any(
        _git(repo, "merge-base", "--is-ancestor", commit, h).returncode == 0
        for h in heads
    )


def _free_leftover_worktree(base: str, held: str, branch: str, known: list) -> str:
    """Free ``branch`` from a leftover (no live session) worktree at ``held``.

    Removed when it holds nothing but PR history (clean, and its HEAD within
    one of the ``known`` PR heads); detached in place otherwise, which keeps
    every changed file and every commit right there. Returns ``held``; raises
    :class:`PRWorktreeUnavailable` if neither works.
    """
    status = _git(held, "status", "--porcelain=v1")
    clean = status.returncode == 0 and not _out(status)
    head = _out(_git(held, "rev-parse", "--verify", "--quiet", "HEAD"))
    if clean and head and _within(base, head, known):
        cp = _git(base, "worktree", "remove", held)
        if cp.returncode != 0:
            cp = _git(base, "worktree", "remove", "--force", held)
        if cp.returncode == 0:
            _git(base, "worktree", "prune")
            _logger.info("removed leftover review worktree %s (%s)", held, branch)
            return held
    cp = _git(held, "checkout", "--detach")
    if cp.returncode != 0:
        raise PRWorktreeUnavailable(
            f"{branch} is held by a leftover worktree at {held}: {_out(cp)}"
        )
    _logger.info(
        "detached leftover worktree %s from %s (its local work stays there)",
        held,
        branch,
    )
    return held
