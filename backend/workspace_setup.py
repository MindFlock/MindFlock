"""Generic, config-driven workspace setup: setup commands + warm cache seeding.

MindFlock provisions many short-lived workspaces (per-story clones, per-PR
checkouts, engine worktrees). Historically the setup steps were hardcoded for
this repo's Python stack (``uv sync``, ``pre-commit install``) and the warm
`testmon <https://testmon.org>`_ database was a special case. This module makes
both generic so any project can use them:

* **Setup commands** (``[workspace].setup_commands``): shell commands run in a
  fresh workspace after checkout, in order. When unset, sensible defaults are
  auto-detected from the workspace contents (``uv sync --all-groups`` for a
  Python/uv project, ``uv run pre-commit install`` when a pre-commit config is
  present) — which preserves MindFlock's historical behaviour with zero config.
* **Cache seeds** (``[[workspace.cache]]``): a host-side warm artifact (a
  testmon DB, a build cache, a compiled-deps tarball, ...) copied into every
  fresh workspace so work starts warm instead of cold. Seeding NEVER clobbers
  an existing file in the workspace, so an evolving per-workspace cache is
  preserved across re-provisions. Each cache can optionally be kept warm by a
  background refresher (see ``ticket_ingestion.cache_refresher``) that
  periodically rebuilds the artifact against a tracked branch and publishes it
  back to ``seed_path``.
"""

import logging
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_logger = logging.getLogger(__name__)

TESTMON_ENV_NAME = "shared"

# Scratch artifacts MindFlock drops into a workspace (any session type);
# excluded from git via .git/info/exclude so they never show up in the diff
# view or get swept into `git add -A`.
# `.testmondata` is the per-workspace cache-fingerprint DB — untracked and
# workspace-local, so it stays out of commits as well.
WORKSPACE_ARTIFACTS = (
    # Images pasted into a terminal pane (ctrl+V) land here so the agent can
    # read them by path — never commit them.
    ".mindflock_pastes/",
    ".mindflock_prompt.md",
    ".mindflock_launch.sh",
    ".mindflock_started",
    ".mindflock_precommit.lock",
    ".mindflock_commit_status",
    ".mindflock_commit_msg",
    # O2/O3 worktree setup + verification-gate artifacts (web.core.worktree_setup).
    ".mindflock_setup.log",
    ".mindflock_setup.json",
    ".mindflock_check.log",
    ".mindflock_check.json",
    ".testmondata",
)


def exclude_artifacts(directory: Path | str) -> None:
    """Add MindFlock's scratch artifacts to ``.git/info/exclude`` (best-effort).

    Works for plain clones AND linked worktrees (``git rev-parse --git-path``
    resolves the shared common git dir). Safe to call for any session type —
    the guided commit endpoint runs it before ``git add -A`` so the markers
    never get committed.
    """
    directory = Path(directory)
    try:
        cp = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--git-path", "info/exclude"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        # Best-effort: a wedged git (e.g. a hung fsmonitor) must not hang the
        # caller — skip the exclude update rather than raise.
        _logger.debug("workspace: git rev-parse timed out; skipping info/exclude")
        return
    if cp.returncode != 0:
        return
    rel_path = cp.stdout.decode("utf-8", "replace").strip()
    if not rel_path:
        return
    exclude = Path(rel_path)
    if not exclude.is_absolute():
        exclude = directory / exclude
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.is_file() else ""
        additions = [a for a in WORKSPACE_ARTIFACTS if a not in existing]
        if additions:
            with open(exclude, "a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                for a in additions:
                    f.write(a + "\n")
    except OSError as err:  # noqa: BLE001
        _logger.debug("workspace: could not update info/exclude: %s", err)


@dataclass
class CacheSeed:
    """One warm cache artifact seeded into fresh workspaces.

    ``seed_path`` is the host-side warm copy; ``workspace_path`` is where it
    lands inside a workspace (relative to the workspace root). ``env`` holds
    environment variables the cache's tooling needs to be stable across
    workspaces (e.g. testmon keys its fingerprints by env) — they are exported
    in session launchers and pinned into the pre-commit hook.

    The refresh fields drive the optional background refresher: it clones the
    repo once, tracks ``refresh_branch``, runs the workspace setup commands and
    then ``refresh_command`` every ``refresh_interval_seconds``, and publishes
    the resulting ``workspace_path`` artifact back to ``seed_path``.
    """

    name: str
    seed_path: Path
    workspace_path: str
    refresh_enabled: bool = True
    refresh_branch: str = "main"
    refresh_interval_seconds: int = 3600
    refresh_command: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.seed_path = Path(self.seed_path).expanduser()


class WorkspaceConfigError(ValueError):
    """Raised for an invalid ``[workspace]`` config section."""


def refresher_dirname(cache_name: str) -> str:
    """Directory (under workspace_dir) the background refresher works in.

    ``testmon`` maps to the historical ``_testmon_refresher`` so existing
    deployments keep their long-lived refresher workspace (and its warm
    ``.venv``) across the upgrade.
    """
    return f"_{cache_name}_refresher"


def is_refresher_dirname(name: str) -> bool:
    """True for a directory name produced by :func:`refresher_dirname`
    (``_<cache>_refresher``), so a long-lived cache-refresher workspace can be
    told apart from a session workspace."""
    return name.startswith("_") and name.endswith("_refresher")


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------
def parse_setup_commands(raw: dict) -> list[str] | None:
    """Read ``[workspace].setup_commands`` from a parsed config dict.

    Returns ``None`` when unset, which means "auto-detect per workspace"
    (see :func:`resolve_setup_commands`). An explicit empty list disables
    setup commands entirely.
    """
    workspace = raw.get("workspace", {}) or {}
    commands = workspace.get("setup_commands")
    if commands is None:
        return None
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise WorkspaceConfigError("workspace.setup_commands must be a list of strings")
    return [c for c in (cmd.strip() for cmd in commands) if c]


def parse_caches(raw: dict, base_dir: Path | None = None) -> list[CacheSeed]:
    """Read ``[[workspace.cache]]`` entries.

    Relative ``seed_path`` values resolve against ``base_dir`` (the config
    file's directory) when given.
    """
    workspace = raw.get("workspace", {}) or {}
    entries = workspace.get("cache", []) or []
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        raise WorkspaceConfigError("workspace.cache must be an array of tables")

    caches: list[CacheSeed] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise WorkspaceConfigError(f"workspace.cache[{i}] must be a table")
        name = entry.get("name")
        seed_path = entry.get("seed_path")
        workspace_path = entry.get("workspace_path")
        if not name or not isinstance(name, str):
            raise WorkspaceConfigError(f"workspace.cache[{i}].name is required")
        if not seed_path or not isinstance(seed_path, str):
            raise WorkspaceConfigError(f"workspace.cache[{i}].seed_path is required")
        if not workspace_path or not isinstance(workspace_path, str):
            raise WorkspaceConfigError(
                f"workspace.cache[{i}].workspace_path is required"
            )
        wp = Path(workspace_path)
        if wp.is_absolute() or ".." in wp.parts:
            raise WorkspaceConfigError(
                f"workspace.cache[{i}].workspace_path must be a relative path "
                "inside the workspace"
            )
        env = entry.get("env", {}) or {}
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise WorkspaceConfigError(
                f"workspace.cache[{i}].env must be a table of string values"
            )
        refresh_command = entry.get("refresh_command")
        if refresh_command is not None and not isinstance(refresh_command, str):
            raise WorkspaceConfigError(
                f"workspace.cache[{i}].refresh_command must be a string"
            )
        caches.append(
            CacheSeed(
                name=name,
                seed_path=_resolve_seed_path(seed_path, base_dir),
                workspace_path=workspace_path,
                refresh_enabled=bool(entry.get("refresh_enabled", True)),
                refresh_branch=str(entry.get("refresh_branch", "main")),
                refresh_interval_seconds=int(
                    entry.get("refresh_interval_seconds", 3600)
                ),
                refresh_command=refresh_command,
                env=dict(env),
            )
        )
    return caches


def _resolve_seed_path(seed_path: str, base_dir: Path | None) -> Path:
    p = Path(seed_path).expanduser()
    if not p.is_absolute() and base_dir is not None:
        p = base_dir / p
    return p


# ---------------------------------------------------------------------------
# Setup commands
# ---------------------------------------------------------------------------
def auto_setup_commands(directory: Path) -> list[str]:
    """Default setup commands detected from workspace contents.

    Preserves MindFlock's historical Python behaviour for uv projects while
    doing nothing surprising in a non-Python repo (where ``uv sync`` would
    just fail).
    """
    commands: list[str] = []
    is_uv_project = (directory / "uv.lock").is_file() or (
        directory / "pyproject.toml"
    ).is_file()
    if is_uv_project:
        commands.append("uv sync --all-groups")
        if (directory / ".pre-commit-config.yaml").is_file():
            # `--all-groups` matches the sync above. Without it, `uv run` drops to
            # the default group set before running, which (when pre-commit lives
            # in a non-default group like `dev`) uninstalls pre-commit mid-run and
            # fails with "Failed to spawn: pre-commit". Keeping the same group set
            # avoids that churn.
            commands.append("uv run --all-groups pre-commit install")
    return commands


def resolve_setup_commands(configured: list[str] | None, directory: Path) -> list[str]:
    """The commands to actually run: explicit config, else auto-detected."""
    if configured is not None:
        return configured
    return auto_setup_commands(Path(directory))


def run_setup_commands(
    commands: list[str],
    directory: Path,
    *,
    strict: bool = False,
    log_prefix: str = "workspace",
) -> None:
    """Run each setup command in ``directory`` through the shell, in order.

    With ``strict`` a non-zero exit raises :class:`SetupCommandError`;
    otherwise it logs a warning and continues (the workspace is still usable
    for many failures, e.g. an optional formatter that is not installed).
    """
    for command in commands:
        _logger.info("%s: running setup command: %s", log_prefix, command)
        cp = subprocess.run(
            command,
            shell=True,
            cwd=str(directory),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if cp.returncode != 0:
            tail = (cp.stdout or b"").decode("utf-8", "replace").strip()[-500:]
            if strict:
                raise SetupCommandError(
                    f"setup command failed (rc={cp.returncode}): {command}\n{tail}"
                )
            _logger.warning(
                "%s: setup command failed (rc=%d, continuing): %s\n%s",
                log_prefix,
                cp.returncode,
                command,
                tail,
            )


async def run_setup_commands_async(
    commands: list[str],
    directory: Path,
    *,
    strict: bool = False,
    log_prefix: str = "workspace",
) -> None:
    """Async variant of :func:`run_setup_commands` for the ingestion pipeline."""
    import asyncio

    for command in commands:
        _logger.info("%s: running setup command: %s", log_prefix, command)
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(directory),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            tail = (stdout or b"").decode("utf-8", "replace").strip()[-500:]
            if strict:
                raise SetupCommandError(
                    f"setup command failed (rc={proc.returncode}): {command}\n{tail}"
                )
            _logger.warning(
                "%s: setup command failed (rc=%d, continuing): %s\n%s",
                log_prefix,
                proc.returncode,
                command,
                tail,
            )


class SetupCommandError(RuntimeError):
    """Raised when a strict setup command exits non-zero."""


# ---------------------------------------------------------------------------
# Cache seeding
# ---------------------------------------------------------------------------
def seed_caches(
    caches: list[CacheSeed],
    directory: Path,
    *,
    log_prefix: str = "workspace",
) -> None:
    """Copy each warm cache into the workspace — never clobbering.

    An existing file at ``workspace_path`` always wins: a workspace that has
    been evolving its own cache (preserved across pause/resume or PR
    re-provision) must not be reset to the shared baseline.
    """
    directory = Path(directory)
    for cache in caches:
        seed = cache.seed_path
        if not seed.is_file():
            _logger.info(
                "%s: cache '%s' seed not found at %s; starting cold",
                log_prefix,
                cache.name,
                seed,
            )
            continue
        target = directory / cache.workspace_path
        if target.exists():
            _logger.info(
                "%s: cache '%s' already present at %s; not re-seeding",
                log_prefix,
                cache.name,
                target,
            )
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(seed, target)
            _logger.info("%s: seeded cache '%s' from %s", log_prefix, cache.name, seed)
        except OSError as err:
            _logger.warning(
                "%s: failed to seed cache '%s': %s", log_prefix, cache.name, err
            )


def merged_cache_env(caches: list[CacheSeed]) -> dict[str, str]:
    """All cache env vars merged, later entries winning on conflicts."""
    env: dict[str, str] = {}
    for cache in caches:
        env.update(cache.env)
    return env


def pin_cache_env(directory: Path, caches: list[CacheSeed]) -> None:
    """Export each cache's env vars from the installed pre-commit hook.

    Some cache tooling keys its state by environment (testmon fingerprints by
    env, so each per-workspace ``.venv`` path would invalidate the seed without
    a stable ``TESTMON_ENV``). Exporting in the hook (untracked,
    workspace-local) makes every workspace share the same key without touching
    tracked files. Resolves the hook via ``git rev-parse --git-path`` so it
    works for plain clones and linked worktrees alike, and never edits a
    git-tracked hook (which would dirty the diff). Must run after
    ``pre-commit install``.
    """
    env = merged_cache_env(caches)
    if not env:
        return
    directory = Path(directory)
    hook = _git_hook_path(directory)
    if hook is None or not hook.is_file():
        return
    tracked = (
        subprocess.run(
            ["git", "-C", str(directory), "ls-files", "--error-unmatch", str(hook)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )
    if tracked:
        _logger.info("workspace: %s is tracked; not pinning cache env in it", hook)
        return
    text = hook.read_text()
    markers = [
        f"export {key}={shlex.quote(value)}" for key, value in sorted(env.items())
    ]
    missing = [m for m in markers if m not in text]
    if not missing:
        return
    lines = text.splitlines(keepends=True)
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    for marker in reversed(missing):
        lines.insert(insert_at, f"{marker}\n")
    hook.write_text("".join(lines))
    _logger.info("workspace: pinned cache env in %s", hook)


def _git_hook_path(directory: Path) -> Path | None:
    cp = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--git-path", "hooks/pre-commit"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if cp.returncode != 0:
        return None
    out = cp.stdout.decode("utf-8", "replace").strip()
    if not out:
        return None
    p = Path(out)
    return p if p.is_absolute() else directory / p
