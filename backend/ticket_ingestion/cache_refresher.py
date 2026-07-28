"""Background task that keeps a warm cache seed fresh against a tracked branch.

One :class:`CacheRefresher` runs per configured ``[[workspace.cache]]`` entry
that has a ``refresh_command``. Each runs in a dedicated long-lived workspace so incremental
tooling (``uv sync``, build caches) stays warm, executes the workspace setup
commands plus the cache's ``refresh_command``, and publishes the resulting
artifact back to the configured ``seed_path``. Per-PR / per-story workspaces
then start from an up-to-date seed — e.g. ``pytest --testmon`` only re-executes
tests truly impacted by their diff instead of the whole suite.

(Formerly ``testmon_refresher`` — it began life as the testmon-specific
refresher and is now fully generic.)
"""

import asyncio
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from backend.ticket_ingestion._subprocess import run_capture
from backend.ticket_ingestion.config import PipelineConfig
from backend.workspace_setup import (
    CacheSeed,
    refresher_dirname,
    run_setup_commands_async,
)

_logger = logging.getLogger(__name__)

# The 16-byte header every SQLite database starts with. Cache artifacts are
# generic (a testmon DB, a build tarball, ...), so the sqlite-specific healing
# below only engages for files that actually are sqlite databases.
_SQLITE_MAGIC = b"SQLite format 3\x00"
# Sidecars a WAL-mode sqlite DB writes next to itself. Orphaned copies left by an
# interrupted writer make the DB throw "disk I/O error" on the next open.
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")

# Wall-clock caps so a hung subprocess (e.g. a fetch over a stalled TCP
# connection) fails this refresh cycle instead of wedging the refresher forever.
_NETWORK_GIT_TIMEOUT = 600.0  # clone / fetch
_LOCAL_GIT_TIMEOUT = 120.0  # checkout / reset / clean
_REFRESH_COMMAND_TIMEOUT = 2 * 60 * 60  # the refresh command (e.g. a test run)
# Keep only this much stderr for logging — a chatty refresh command must not
# grow an unbounded buffer in memory.
_MAX_RETAINED_STDERR = 64 * 1024


def _looks_like_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _remove_sqlite_sidecars(artifact: Path) -> None:
    """Delete any ``-wal``/``-shm``/``-journal`` files beside ``artifact``."""
    for suffix in _SQLITE_SIDECARS:
        sidecar = artifact.with_name(artifact.name + suffix)
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:  # noqa: BLE001
            _logger.debug("could not remove sidecar %s: %s", sidecar, e)


def _sqlite_intact(path: Path) -> bool:
    """Best-effort: open ``path`` read-only and run a quick integrity check.

    Call after :func:`_remove_sqlite_sidecars` so a WAL-mode DB opens from its
    (complete) main file. Any sqlite error counts as "not intact"."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
        return bool(row) and row[0] == "ok"
    except sqlite3.Error:
        return False


def _checkpoint_and_verify(path: Path) -> bool:
    """Fold any WAL into the main file, then verify it — used before publishing.

    Publishing copies only the main artifact file, so an un-checkpointed WAL
    would silently drop rows; a TRUNCATE checkpoint folds them in first. Returns
    False (skip publish) if the DB can't be opened/checkpointed/verified."""
    try:
        con = sqlite3.connect(str(path), timeout=30)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
        return bool(row) and row[0] == "ok"
    except sqlite3.Error:
        return False


class CacheRefresher:
    def __init__(self, config: PipelineConfig, cache: CacheSeed) -> None:
        self.config = config
        self.cache = cache
        self.directory = Path(config.workspace_dir) / refresher_dirname(cache.name)

    async def run_forever(self) -> None:
        if not self.cache.refresh_command:
            _logger.info(
                "Cache '%s' has no refresh_command; refresher disabled",
                self.cache.name,
            )
            return
        _logger.info(
            "Cache refresher starting: cache=%s branch=%s interval=%ds dir=%s seed=%s",
            self.cache.name,
            self.cache.refresh_branch,
            self.cache.refresh_interval_seconds,
            self.directory,
            self.cache.seed_path,
        )
        while True:
            try:
                await self._refresh_once()
            except Exception as e:
                _logger.exception(
                    "Cache '%s' refresh cycle failed: %s", self.cache.name, e
                )
            await asyncio.sleep(self.cache.refresh_interval_seconds)

    async def _refresh_once(self) -> None:
        await self._ensure_workspace()
        await self._sync_to_branch()
        self._heal_cache_artifact()
        # Only run EXPLICITLY-configured setup commands. Auto-detected Python
        # setup (`uv sync --all-groups` + `uv run pre-commit install`) is meant
        # for agent workspaces and actively breaks a `uv run --group <g>` refresh
        # command: after `uv sync --all-groups` the refresh command's `uv run
        # --group test` transitions the env to a narrower group set and unlinks
        # the very tool it then tries to spawn (observed live as
        # "Failed to spawn: pytest"). A `uv run`-based refresh command provisions
        # its own environment (exactly as CI does), so auto setup is both
        # redundant and harmful here — skip it. Explicit `setup_commands` (incl.
        # an empty list) are still honoured for refreshers that genuinely need a
        # build step first.
        if self.config.setup_commands is not None:
            await run_setup_commands_async(
                self.config.setup_commands,
                self.directory,
                log_prefix=f"refresher[{self.cache.name}]",
            )
        else:
            _logger.info(
                "refresher[%s]: skipping auto-detected setup — the refresh "
                "command provisions its own environment",
                self.cache.name,
            )
        await self._run_refresh_command()
        await self._publish_seed()

    async def _ensure_workspace(self) -> None:
        if (self.directory / ".git").is_dir():
            return
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        _logger.info("Cloning refresher workspace into %s", self.directory)
        await self._check_run(
            "git",
            "clone",
            self.config.repo_url,
            str(self.directory),
            cwd=None,
            err="git clone (refresher)",
            timeout=_NETWORK_GIT_TIMEOUT,
        )
        seed = self.cache.seed_path
        if seed.is_file():
            target = self.directory / self.cache.workspace_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(seed, target)
            _logger.info("Seeded refresher workspace from %s", seed)

    async def _sync_to_branch(self) -> None:
        cwd = str(self.directory)
        branch = self.cache.refresh_branch
        await self._check_run(
            "git",
            "fetch",
            "origin",
            branch,
            cwd=cwd,
            err="git fetch",
            timeout=_NETWORK_GIT_TIMEOUT,
        )
        await self._check_run(
            "git",
            "checkout",
            "-B",
            branch,
            f"origin/{branch}",
            cwd=cwd,
            err="git checkout",
        )
        await self._check_run(
            "git",
            "reset",
            "--hard",
            f"origin/{branch}",
            cwd=cwd,
            err="git reset",
        )
        await self._check_run(
            "git",
            "clean",
            "-fdx",
            "-e",
            ".venv",
            "-e",
            self.cache.workspace_path,
            cwd=cwd,
            err="git clean",
        )

    def _heal_cache_artifact(self) -> None:
        """Recover the workspace's cache artifact from an interrupted prior run.

        A killed writer (``pytest --testmon`` cut short by an ingestion stop, a
        sleep, an OOM, ...) can leave orphaned sqlite sidecars whose mismatch with
        the main file makes the next open throw ``disk I/O error``; and because
        testmon opens with ``synchronous = OFF`` a hard kill can leave the main
        file itself unreadable — which would then fail every cycle since the
        artifact is preserved across syncs. So: clear stale sidecars, and if the
        artifact is a sqlite DB that no longer passes a quick check, discard it
        and restore the last-published seed (else start cold). No-op for
        non-sqlite artifacts."""
        artifact = self.directory / self.cache.workspace_path
        if not artifact.is_file():
            return
        _remove_sqlite_sidecars(artifact)
        if not _looks_like_sqlite(artifact) or _sqlite_intact(artifact):
            return
        _logger.warning(
            "refresher[%s]: cache artifact %s is unreadable (likely an "
            "interrupted prior run); discarding it and restoring the last seed",
            self.cache.name,
            artifact,
        )
        try:
            artifact.unlink()
        except OSError:
            pass
        seed = self.cache.seed_path
        if seed.is_file():
            try:
                shutil.copy2(seed, artifact)
            except OSError as e:  # noqa: BLE001
                _logger.warning(
                    "refresher[%s]: could not restore seed after discarding a "
                    "bad artifact: %s",
                    self.cache.name,
                    e,
                )

    async def _run_refresh_command(self) -> None:
        assert self.cache.refresh_command is not None
        _logger.info(
            "Refresher[%s] running: %s", self.cache.name, self.cache.refresh_command
        )
        env = os.environ.copy()
        env.update(self.cache.env)
        proc = await asyncio.create_subprocess_shell(
            self.cache.refresh_command,
            cwd=str(self.directory),
            env=env,
            # stdout is never used — discard it instead of buffering a whole
            # test run's output in memory; stderr is trimmed after read.
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_REFRESH_COMMAND_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.communicate()  # reap the killed process
            except ProcessLookupError:
                pass
            raise RuntimeError(
                f"Refresh command for cache '{self.cache.name}' timed out "
                f"after {_REFRESH_COMMAND_TIMEOUT}s"
            )
        stderr = stderr[-_MAX_RETAINED_STDERR:]
        rc = proc.returncode or 0
        # A refresh command may exit non-zero while still producing a valid
        # artifact (e.g. pytest --testmon exits 1 on test failures but the
        # fingerprint DB is a legitimate update). Escalation happens in
        # _publish_seed: no artifact at all means the run was a hard failure.
        if rc != 0:
            msg = stderr.decode(errors="replace").strip()[-500:]
            _logger.warning(
                "Refresher[%s] command exited rc=%d: %s", self.cache.name, rc, msg
            )
            if not (self.directory / self.cache.workspace_path).is_file():
                raise RuntimeError(
                    f"Refresh command for cache '{self.cache.name}' failed "
                    f"(rc={rc}) and produced no artifact: {msg}"
                )

    async def _publish_seed(self) -> None:
        src = self.directory / self.cache.workspace_path
        if not src.is_file():
            _logger.warning(
                "No %s produced; skipping seed publish for cache '%s'",
                self.cache.workspace_path,
                self.cache.name,
            )
            return
        # For a sqlite artifact: fold any WAL into the main file (publishing
        # copies only the main file, so un-checkpointed rows would be lost) and
        # verify it. A crashed refresh can leave a corrupt DB present; publishing
        # it would poison the shared seed for every future workspace, so keep the
        # previous good seed instead. No-op for non-sqlite artifacts.
        if _looks_like_sqlite(src) and not _checkpoint_and_verify(src):
            _logger.warning(
                "refresher[%s]: refreshed artifact failed verification; keeping "
                "the previous seed at %s",
                self.cache.name,
                self.cache.seed_path,
            )
            return
        dst = self.cache.seed_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        _logger.info(
            "Published refreshed '%s' seed to %s (%d bytes)",
            self.cache.name,
            dst,
            dst.stat().st_size,
        )

    async def _run(
        self,
        *args: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = _LOCAL_GIT_TIMEOUT,
    ) -> tuple[int, bytes, bytes]:
        # The rc-124 timeout convention lives in the shared helper so it can't
        # drift from the provisioner / PR-provisioner copies.
        return await run_capture(*args, cwd=cwd, env=env, timeout=timeout)

    async def _check_run(
        self,
        *args: str,
        cwd: str | None,
        err: str,
        timeout: float = _LOCAL_GIT_TIMEOUT,
    ) -> None:
        rc, _, stderr = await self._run(*args, cwd=cwd, timeout=timeout)
        if rc != 0:
            msg = stderr.decode(errors="replace").strip()[-500:]
            raise RuntimeError(f"{err} failed (rc={rc}): {msg}")
