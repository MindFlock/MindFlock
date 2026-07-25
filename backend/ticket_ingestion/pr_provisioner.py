"""Provision a workspace for a PR: clone (or fetch) the configured repo and check out the PR head branch."""

import asyncio
import logging
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from backend.ticket_ingestion._subprocess import run_capture
from backend.ticket_ingestion.config import PipelineConfig
from backend.ticket_ingestion.models import (
    ProvisionedPRWorkspace,
    PullRequest,
    pr_slug,
)
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
        self, pr: PullRequest, launch_cursor: bool = True
    ) -> ProvisionedPRWorkspace:
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
        # Clone the PR's own repo (multi-repo review).
        clone_url = pr.clone_url
        # Blobless partial clone (matches the ticket path's canonical clone in
        # session/provisioned.py): fetches commit/tree objects now and blobs on
        # demand, which is dramatically faster than a full clone of a large repo
        # — that plain `git clone` was the bulk of PR provisioning time. `--no-tags`
        # skips the tag refs we never use. Full history is preserved so diffs
        # against the base branch still work.
        rc, _, stderr = await _run(
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
            rc, _, stderr = await _run(
                "git",
                "clone",
                clone_url,
                str(directory),
                timeout=_NETWORK_GIT_TIMEOUT,
            )
        if rc != 0:
            raise PRProvisioningError(
                f"git clone failed for PR #{pr.number}: {stderr.decode(errors='replace').strip()}"
            )
        await self._checkout_pr_head(pr, directory)

    async def _refresh(self, pr: PullRequest, directory: Path) -> None:
        await self._checkout_pr_head(pr, directory)

    async def _checkout_pr_head(self, pr: PullRequest, directory: Path) -> None:
        # Fetch the PR head via refs/pull/<n>/head, which GitHub exposes for both
        # same-repo and fork PRs. This avoids depending on origin/<head_ref>, which
        # does not exist when the branch lives on a fork.
        rc, _, stderr = await _run(
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
                f"{stderr.decode(errors='replace').strip()}"
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
