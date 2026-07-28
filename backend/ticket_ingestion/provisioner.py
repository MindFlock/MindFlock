"""Environment provisioner: clone repo, uv sync, branch, launch Cursor."""

import asyncio
import logging
import re
import shutil
from pathlib import Path

from backend.ticket_ingestion._subprocess import run_capture
from backend.ticket_ingestion.config import PipelineConfig
from backend.ticket_ingestion.models import ProvisionedEnvironment, Ticket
from backend.workspace_setup import (
    pin_cache_env,
    resolve_setup_commands,
    run_setup_commands_async,
    seed_caches,
)

_logger = logging.getLogger(__name__)


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "story"


def _branch_name_for(story: Ticket) -> str:
    # feature/<slug>/<name>. For Shortcut the slug is sc-<id>, so this stays
    # byte-identical to the historic feature/sc-<id>/<name> scheme.
    return f"feature/{story.slug}/{_slugify(story.name)}"


_WINDOW_POLL_ATTEMPTS = 30
_WINDOW_POLL_INTERVAL = 1.0

# Wall-clock caps so a hung subprocess (e.g. a clone over a stalled TCP
# connection) fails the story instead of wedging the whole pipeline forever.
_NETWORK_GIT_TIMEOUT = 600.0  # clone / anything hitting the network
_LOCAL_CMD_TIMEOUT = 120.0  # local git ops and small helper commands


class ProvisioningError(Exception):
    """Raised when provisioning a workspace fails."""


class EnvironmentProvisioner:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    async def provision(self, story: Ticket) -> ProvisionedEnvironment:
        branch_name = _branch_name_for(story)
        directory = Path(self.config.workspace_dir) / branch_name.replace("/", "-")
        _logger.info(
            "Provisioning workspace for story %d at %s (branch: %s)",
            story.id,
            directory,
            branch_name,
        )

        await self._git_clone(story, directory)
        await self._git_checkout(story, directory, branch_name)
        await self._run_setup_commands(story, directory)
        pin_cache_env(directory, self.config.caches)
        seed_caches(self.config.caches, directory, log_prefix=f"story {story.id}")
        await self._launch_cursor(story, directory)
        await self._install_precommit_log_wrapper(story, directory)
        window_id = 0
        _logger.info("Workspace ready for story %d.", story.id)

        return ProvisionedEnvironment(
            directory=directory,
            branch_name=branch_name,
            cursor_window_id=window_id,
        )

    async def _run(
        self,
        *args: str,
        cwd: str | None = None,
        timeout: float = _LOCAL_CMD_TIMEOUT,
    ) -> tuple[int, bytes, bytes]:
        # The rc-124 timeout convention lives in the shared helper so it can't
        # drift from the PR-provisioner / cache-refresher copies (a timed-out
        # command still surfaces as a normal ProvisioningError to callers).
        return await run_capture(*args, cwd=cwd, timeout=timeout)

    async def _git_clone(self, story: Ticket, directory: Path) -> None:
        if directory.exists():
            _logger.info(
                "Removing existing workspace at %s for story %d", directory, story.id
            )
            shutil.rmtree(directory)
        # Clone into a sibling temp dir to avoid races with background processes
        # (e.g. Excalidraw MCP, Cursor) that may recreate `directory` mid-clone
        # and drop log files into it, which would break `git clone <existing-nonempty>`.
        tmp_dir = directory.with_name(directory.name + ".clone-tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        directory.parent.mkdir(parents=True, exist_ok=True)
        # Multi-repo ingestion: clone the ticket's own source repo when set,
        # else the global default.
        clone_url = getattr(story, "repo_url", "") or self.config.repo_url
        _logger.info("Cloning %s for story %d into %s", clone_url, story.id, directory)
        rc, _, stderr = await self._run(
            "git",
            "clone",
            "--depth=1",
            clone_url,
            str(tmp_dir),
            timeout=_NETWORK_GIT_TIMEOUT,
        )
        if rc != 0:
            msg = stderr.decode(errors="replace").strip()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise ProvisioningError(f"Git clone failed for story {story.id}: {msg}")
        if directory.exists():
            shutil.rmtree(directory)
        tmp_dir.rename(directory)

    async def _run_setup_commands(self, story: Ticket, directory: Path) -> None:
        """Run the configured (or auto-detected) workspace setup commands.

        Failures warn and continue: a workspace with a failed optional step is
        still useful to the agent, which can re-run the step itself with the
        error in front of it.
        """
        commands = resolve_setup_commands(self.config.setup_commands, directory)
        await run_setup_commands_async(
            commands, directory, log_prefix=f"story {story.id}"
        )

    async def _git_checkout(
        self, story: Ticket, directory: Path, branch_name: str
    ) -> None:
        _logger.info("Creating branch %s for story %d", branch_name, story.id)
        rc, _, stderr = await self._run(
            "git", "checkout", "-B", branch_name, cwd=str(directory)
        )
        if rc != 0:
            msg = stderr.decode(errors="replace").strip()
            raise ProvisioningError(f"git checkout failed for story {story.id}: {msg}")

    async def _launch_cursor(self, story: Ticket, directory: Path) -> None:
        from backend.config import ide as _ide
        from backend.web.core import ide_launch as _ide_launch

        _logger.info("Opening %s for story %d", _ide.ide_name(), story.id)
        try:
            # launch_ide is sync (fire-and-forget Popen); run it off-loop.
            await asyncio.to_thread(_ide_launch.launch_ide, str(directory))
        except Exception as err:  # noqa: BLE001
            raise ProvisioningError(
                f"{_ide.ide_name()} launch failed for story {story.id}: {err}"
            )

    async def _install_precommit_log_wrapper(
        self, story: Ticket, directory: Path
    ) -> None:
        script = directory / "auto_fix_precommit_hook.py"
        if not script.is_file():
            _logger.info(
                "No auto_fix_precommit_hook.py at %s; skipping log wrapper install for story %d",
                script,
                story.id,
            )
            return
        _logger.info("Installing pre-commit log wrapper for story %d", story.id)
        rc, _, stderr = await self._run(
            "uv",
            "run",
            "python",
            script.name,
            cwd=str(directory),
            timeout=_NETWORK_GIT_TIMEOUT,  # uv may resolve/download packages
        )
        if rc != 0:
            msg = stderr.decode(errors="replace").strip()
            _logger.warning(
                "auto_fix_precommit_hook.py failed for story %d (continuing): %s",
                story.id,
                msg,
            )
