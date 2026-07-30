"""Provisioned workspaces for mindflock sessions.

This module gives any session an *opt-in* "fully-loaded, fast-testing
workspace". None of it runs unless a session is created with
``InstanceOptions.provisioned=True`` — ordinary sessions are completely
unaffected.

What a provisioned session gets:

  * a **deterministic branch** (``feature/sc-<id>/<slug>`` for a Shortcut
    story, or ``mindflock/<title>`` for an ad-hoc warm workspace),
  * the target repo checked out (worktree off a canonical base clone, *or* a
    fresh ``git clone`` — selectable per session via ``workspace_strategy``),
  * the configured (or auto-detected) **workspace setup commands** — for a
    Python/uv project that means ``uv sync --all-groups`` + ``pre-commit
    install``,
  * every configured **warm cache seed** copied in (see
    :mod:`backend.workspace_setup`) with its env pinned in the installed
    pre-commit hook — e.g. a testmon seed (``.testmondata``) plus
    ``TESTMON_ENV``, so ``pytest --testmon`` only runs diff-impacted tests.

The target repo is either the configured ``[repository].url`` from
``config.toml`` (:func:`load_provision_settings`) or — universally — **any
local git repo** (:func:`local_settings_for`), in which case setup commands are
auto-detected from the workspace contents and no shared cache seeds apply.

Two worktree strategies are provided as drop-in replacements for the engine's
``GitWorktree`` (they duck-type its full method surface so ``Instance`` /
``backend.web.server`` need no special-casing beyond constructing the right
object):

  * :class:`ProvisionedWorktree`      — *worktree* strategy (default): a git
    worktree off a canonical per-repo base clone. Fast, disk-efficient, and
    native to mindflock's pause/resume/diff lifecycle.
  * :class:`ProvisionedCloneWorktree` — *clone* strategy: a full standalone
    ``git clone`` per session. Strongest git-level isolation; the clone is
    preserved across pause (Remove is a no-op).

Both run the *same* provisioning sequence, so the resulting branch is identical
and idempotent — you can run the full suite the same way in either.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from backend import workspace_setup
from backend.session.git import remote_url
from backend.session.git.worktree import (
    GitWorktree,
    resolve_worktree_paths,
)
from backend.session.git.util import sanitize_branch_name
from backend.workspace_setup import CacheSeed

__all__ = [
    "ProvisionError",
    "ProvisionSettings",
    "load_provision_settings",
    "provisioning_available",
    "local_settings_for",
    "settings_for_workspace",
    "forge_origin",
    "point_origin_at_forge",
    "default_workspace_dir",
    "slugify",
    "branch_name_for",
    "repo_display_name",
    "base_repo_dirname",
    "is_base_repo_dirname",
    "resolve_base_repo_dir",
    "ensure_base_repo",
    "provision_workspace",
    "write_launcher",
    "ProvisionedWorktree",
    "ProvisionedCloneWorktree",
    "build_provisioned_worktree",
    "LAUNCHER_BASENAME",
]

_logger = logging.getLogger("mindflock.provision")

# Canonical base clones live under the workspace dir, one per repo:
# ``_base_<repo-slug>``.
BASE_REPO_PREFIX = "_base_"

# Constant testmon env key. testmon fingerprints its env by the absolute path of
# sys.executable; because every per-workspace `.venv` lives at a different path,
# the seeded `.testmondata` would otherwise be invalidated. Exporting a fixed
# TESTMON_ENV in the (untracked) pre-commit hook makes every workspace share one
# testmon env key without touching any tracked file. (Shared with the generic
# cache-seed primitive in backend.workspace_setup.)
TESTMON_ENV_NAME = workspace_setup.TESTMON_ENV_NAME

# Launcher script written into every provisioned workspace; the web server's
# reboot-resume reads it back.
LAUNCHER_BASENAME = ".mindflock_launch.sh"

# Workspace-local marker files:
#   .mindflock_prompt.md  — persisted seed prompt
#   .mindflock_started    — first-launch guard
# The git-exclude list for them lives in
# ``workspace_setup.WORKSPACE_ARTIFACTS`` so they never show up in diffs or
# commits.
PROMPT_BASENAME = ".mindflock_prompt.md"
STARTED_MARKER = ".mindflock_started"


class ProvisionError(Exception):
    """Raised when workspace provisioning fails in a way that should abort."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass
class ProvisionSettings:
    """Everything provisioning needs, distilled from MindFlock config (or a
    local repo).

    ``workspace_dir`` holds both the canonical base clones (worktree strategy)
    and the per-session clones (clone strategy). ``base_branch`` is the fork
    point for new branches and the branch any cache seed is built against.
    ``repo_url`` may be a remote URL *or* an absolute local repo path
    (:func:`local_settings_for`) — it is the CLONE SOURCE.

    ``origin_url`` is the forge URL the provisioned workspace's ``origin`` must
    end up pointing at, which is NOT always the clone source. Cloning from the
    user's own checkout is fast and works offline, but it left ``origin`` set to
    a path on their laptop: pushes went to the local repo, reported success, and
    no branch ever reached the forge. So the local path stays the clone source
    and ``origin`` is re-pointed at this URL afterwards. Empty means "leave
    origin alone" — an offline repo with no forge remote keeps working exactly
    as before.
    """

    repo_url: str
    workspace_dir: Path
    origin_url: str = ""
    base_branch: str = "staging"
    open_cursor: bool = False
    skip_permissions: bool = True
    # Generic workspace setup: shell commands run on provision (None =
    # auto-detect from workspace contents) and warm cache seeds.
    setup_commands: Optional[List[str]] = None
    caches: List[CacheSeed] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.workspace_dir = Path(self.workspace_dir).expanduser()


def _candidate_config_paths() -> List[Path]:
    """Where to look for MindFlock's ``config.toml``, most specific first."""
    paths: List[Path] = []
    env = os.environ.get("MINDFLOCK_CONFIG")
    if env:
        paths.append(Path(env).expanduser())
    # Current working directory (the repo the server was launched in).
    paths.append(Path.cwd() / "config.toml")
    # The MindFlock repo root: this file is at
    # <repo>/backend/session/provisioned.py -> parents[2] == <repo>.
    try:
        repo_root = Path(__file__).resolve().parents[2]
        paths.append(repo_root / "config.toml")
    except IndexError:  # pragma: no cover - defensive
        pass
    # De-dupe while preserving order.
    seen: set = set()
    out: List[Path] = []
    for p in paths:
        rp = p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
        if rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    return out


def _find_config() -> Optional[Path]:
    for p in _candidate_config_paths():
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _read_config_toml(path: Path) -> dict:
    """Parse a MindFlock ``config.toml`` into a raw dict, tolerating failures.

    Uses the stdlib ``tomllib`` (Python 3.11+), falling back to ``tomli`` on
    older interpreters. Any read/parse error is logged and yields an empty dict
    so provisioning can still fall back to the settings-store / env layers.
    """
    try:
        import tomllib as _toml  # Python 3.11+

        with open(path, "rb") as f:
            return _toml.load(f)
    except ModuleNotFoundError:  # pragma: no cover - older interpreters
        try:
            import tomli as _toml  # type: ignore

            with open(path, "rb") as f:
                return _toml.load(f)
        except Exception as err:  # noqa: BLE001
            _logger.warning("provision: failed to read %s: %s", path, err)
            return {}
    except Exception as err:  # noqa: BLE001
        _logger.warning("provision: failed to read %s: %s", path, err)
        return {}


def load_provision_settings(
    config_path: Optional[Path] = None,
    repo_url_override: Optional[str] = None,
) -> Optional[ProvisionSettings]:
    """Build :class:`ProvisionSettings` from MindFlock's ``config.toml``.

    Reads the keys provisioning needs (``repository.url`` /
    ``repository.workspace_dir`` / the ``[workspace]`` setup + cache section /
    ``github.base_branch`` / the engine block) and layers the user settings
    store + env over them (``env → settings.json → config.toml``). Tolerates an
    incomplete or entirely absent ``config.toml``: if the settings store
    supplies ``repository.url`` provisioning is still available. Returns
    ``None`` (and logs) only when no ``repository.url`` can be resolved from
    any layer — sessions on an explicit local repo use
    :func:`local_settings_for` instead.
    """
    path = Path(config_path) if config_path is not None else _find_config()
    have_file = path is not None and Path(path).is_file()

    raw: dict = _read_config_toml(path) if have_file else {}

    # Base dir for resolving relative paths: the config file's dir when we have
    # one, else the current working directory (the repo root MindFlock runs from).
    base_dir = Path(path).resolve().parent if have_file else Path.cwd()

    repository = raw.get("repository", {}) or {}
    github = raw.get("github", {}) or {}
    cs_section = raw.get("mindflock") or {}

    from backend.config import settings as _s

    # An explicit per-session override (multi-repo ticket ingestion) wins over
    # every config/settings/env layer; only the repo changes, the rest of the
    # provisioning settings stay global.
    repo_url = (repo_url_override or "").strip() or _s.resolve_str(
        env="MINDFLOCK_REPO_URL",
        settings_getter=lambda s: s.repository.url,
        toml_value=repository.get("url"),
    )
    if not repo_url:
        _logger.info(
            "provision: no [repository].url resolved (config.toml / settings / env); "
            "configured-repo provisioning unavailable"
        )
        return None

    workspace_dir = _s.resolve_str(
        env="MINDFLOCK_WORKSPACE_DIR",
        settings_getter=lambda s: s.repository.workspace_dir,
        toml_value=repository.get("workspace_dir"),
        default="./workspaces",
    )
    # Resolve a relative workspace_dir against the config file's dir (or cwd when
    # configured entirely from the settings store).
    ws = Path(workspace_dir).expanduser()
    if not ws.is_absolute():
        ws = (base_dir / ws).resolve()

    # Generic workspace setup + cache seeds ([workspace] section).
    try:
        setup_commands = workspace_setup.parse_setup_commands(raw)
        caches = workspace_setup.parse_caches(raw, base_dir)
    except workspace_setup.WorkspaceConfigError as err:
        _logger.warning("provision: invalid [workspace] config (%s); ignoring", err)
        setup_commands, caches = None, []

    base_branch = _s.resolve_str(
        env="MINDFLOCK_BASE_BRANCH",
        settings_getter=lambda s: s.repository.base_branch or s.github.base_branch,
        toml_value=github.get("base_branch"),
        default="main",
    )

    open_cursor = _s.resolve_bool(
        settings_getter=lambda s: s.engine.open_cursor,
        toml_value=cs_section.get("open_cursor"),
        default=False,
    )
    skip_permissions = _s.resolve_bool(
        settings_getter=lambda s: s.engine.skip_permissions,
        toml_value=cs_section.get("skip_permissions"),
        default=True,
    )

    return ProvisionSettings(
        repo_url=str(repo_url),
        workspace_dir=ws,
        base_branch=str(base_branch),
        open_cursor=bool(open_cursor),
        skip_permissions=bool(skip_permissions),
        setup_commands=setup_commands,
        caches=caches,
    )


def provisioning_available() -> bool:
    """Whether configured-repo provisioning can be offered (config present +
    usable). Local-repo provisioning (:func:`local_settings_for`) is always
    available for any git repo, so this only gates the no-repo-chosen flow."""
    return load_provision_settings() is not None


def default_workspace_dir() -> Path:
    """The workspace dir for provisioned checkouts: the configured
    ``[repository].workspace_dir`` when config resolves, else
    ``~/.mindflock/workspaces``."""
    s = load_provision_settings()
    if s is not None:
        return s.workspace_dir
    return Path.home() / ".mindflock" / "workspaces"


def _git_current_branch(repo: str | Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.decode("utf-8", "replace").strip()


def _git_origin_url(repo: str | Path) -> str:
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.decode("utf-8", "replace").strip()


def local_settings_for(repo_path: str | Path) -> Optional[ProvisionSettings]:
    """Provision settings for an arbitrary LOCAL git repo (universal flow).

    ``repo_url`` is the absolute repo path (``git clone`` / ``git worktree``
    both accept local paths), ``base_branch`` is the repo's current branch, and
    setup commands are auto-detected per workspace
    (:func:`backend.workspace_setup.auto_setup_commands`). The globally
    configured cache seeds are NOT applied — they are built against the
    configured repo and would be wrong for any other one. Returns ``None`` when
    ``repo_path`` is not a git repo.

    ``origin_url`` carries the source repo's OWN origin so provisioned
    workspaces push to the forge rather than back into this checkout (see
    :func:`point_origin_at_forge`). It is copied verbatim — an SSH remote stays
    SSH — and is left empty for a repo with no origin, which keeps a purely
    local repo working offline exactly as it did.
    """
    repo = Path(repo_path).expanduser()
    if not (repo / ".git").exists():
        return None
    branch = _git_current_branch(repo) or "main"
    return ProvisionSettings(
        repo_url=str(repo.resolve()),
        origin_url=_git_origin_url(repo),
        workspace_dir=default_workspace_dir(),
        base_branch=branch,
        open_cursor=False,
        skip_permissions=True,
        setup_commands=None,  # auto-detect per workspace
        caches=[],
    )


def _same_repo_url(a: str, b: str) -> bool:
    """Whether two repo URLs/paths name the same repo.

    Transport-independent first: ``git@github.com:Org/app.git`` and
    ``https://github.com/Org/app`` are one repo, and a literal compare says
    otherwise — which is how a user whose checkout is SSH and whose
    ``[repository].url`` is the HTTPS spelling (or the reverse) silently lost
    their configured settings, base branch included.

    The old normalize-and-compare stays as the FALLBACK rather than being
    replaced: :func:`remote_url.same_repo` answers False whenever either side is
    a LOCAL PATH (there is no forge behind one), and MindFlock legitimately
    provisions from local paths — matching two spellings of the same local path
    is still this function's job.
    """
    if not a:
        return False
    if remote_url.same_repo(a, b):
        return True

    def norm(u: str) -> str:
        u = (u or "").strip().rstrip("/")
        if u.endswith(".git"):
            u = u[: -len(".git")]
        return u

    return norm(a) == norm(b)


def forge_origin(settings: ProvisionSettings) -> str:
    """The forge URL a workspace provisioned from ``settings`` should push to.

    ``origin_url`` wins (the universal flow sets it from the source repo's own
    origin); otherwise ``repo_url`` itself, which IS a forge URL whenever the
    clone source is remote — the configured and ingestion flows. Returns ``""``
    when neither names a forge, i.e. a purely local repo with no upstream, where
    there is nothing better to point origin at than the clone source.
    """
    for candidate in (settings.origin_url, settings.repo_url):
        u = (candidate or "").strip()
        if u and remote_url.parse_remote(u) is not None:
            return u
    return ""


def point_origin_at_forge(repo_dir: str | Path, settings: ProvisionSettings) -> None:
    """Re-point ``repo_dir``'s ``origin`` at the forge. Idempotent, best-effort.

    Provisioning clones from the user's own checkout because it is fast and
    works offline, but that leaves ``origin`` pointing at a directory on their
    laptop. Pushes then "succeed" into the local repo: the branch never reaches
    the forge, the stage chip still flips to ``pushed``, and Make PR fails
    against a remote that is not a GitHub repo. Cloning locally and pushing
    remotely is the point of splitting clone-source from origin.

    Called on both create and refresh, so a base clone left over from before
    this fix is healed on its next use rather than needing a manual reset. A
    failure here is logged and tolerated: a workspace with the old origin is
    exactly as broken as it was before, and never worse.
    """
    forge = forge_origin(settings)
    if not forge:
        return
    current = _git_origin_url(repo_dir)
    if current == forge:
        return
    cp = _run("git", "-C", str(repo_dir), "remote", "set-url", "origin", forge)
    if cp.returncode != 0:
        _logger.warning(
            "provision: could not re-point origin of %s at %s — pushes from this "
            "workspace may go to %s instead of the forge",
            repo_dir,
            forge,
            current or "(unset)",
        )
        return
    if current:
        _logger.info(
            "provision: origin of %s re-pointed from %s to %s",
            repo_dir,
            current,
            forge,
        )


def settings_for_workspace(repo_path: str) -> Optional[ProvisionSettings]:
    """Best settings for re-provisioning a persisted workspace whose base repo
    (worktree ``repoPath`` / clone dir) is ``repo_path``.

    Prefers the configured settings when the workspace's ``origin`` matches the
    configured repo (or can't be read); otherwise falls back to local-repo
    settings derived from the origin (a local path) or the workspace itself.

    ``origin`` is matched against the configured ``origin_url`` as well as
    ``repo_url``: since a provisioned workspace's origin is now the FORGE and
    its clone source may have been a local path, comparing against the clone
    source alone would stop recognising the workspace as the configured repo.
    """
    settings = load_provision_settings()
    origin = _git_origin_url(repo_path) if repo_path else ""
    if settings is not None and (
        not origin
        or _same_repo_url(origin, settings.repo_url)
        or (settings.origin_url and _same_repo_url(origin, settings.origin_url))
    ):
        return settings
    if origin and os.path.isdir(origin):
        local = local_settings_for(origin)
        if local is not None:
            return local
    if settings is not None:
        return settings
    return local_settings_for(repo_path) if repo_path else None


# ---------------------------------------------------------------------------
# Branch naming
# ---------------------------------------------------------------------------
def slugify(text: str, max_len: int = 40) -> str:
    """Lower-case, dash-collapsed slug (mirrors the pipeline's ``_slugify``)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "story"


def branch_name_for(
    story_id: Optional[str | int], title: str, name: Optional[str] = None
) -> str:
    """Compute the branch name for a provisioned session.

    With a Shortcut story id -> ``feature/sc-<id>/<slug>`` (the pipeline's
    scheme, slugged from ``name`` or ``title``). Without one ->
    ``mindflock/<title>``.

    The story id is sanitized to a safe ref token, and a slug that just repeats
    ``sc-<id>`` (e.g. when the title was auto-derived as ``sc-<id>``) falls back
    to ``story`` so the branch reads ``feature/sc-<id>/story`` rather than
    ``feature/sc-<id>/sc-<id>``.
    """
    if story_id is not None and str(story_id).strip() != "":
        sid = re.sub(r"[^A-Za-z0-9_.-]+", "", str(story_id).strip()) or "story"
        slug = slugify(name or title)
        if not slug or slug == f"sc-{sid}" or slug == sid:
            slug = "story"
        return f"feature/sc-{sid}/{slug}"
    return f"mindflock/{slugify(title)}"


# ---------------------------------------------------------------------------
# Small subprocess helpers (synchronous — called from Instance.Start, which the
# backend.web already runs off the event loop via asyncio.to_thread).
# ---------------------------------------------------------------------------
#: Default subprocess budgets (seconds). Local git commands get the default;
#: network operations (clone/fetch) and setup commands pass a larger one.
_RUN_TIMEOUT: float = 60.0
_NET_TIMEOUT: float = 600.0


def _noninteractive_env(env: Optional[dict] = None) -> dict:
    """``env`` (or the inherited environment) plus git's fail-fast settings.

    Provisioning shells out to git from a server with no terminal attached.
    Without this, a clone/fetch that needs a credential parks on a prompt
    against whatever stdin we inherited and sits there until ``_NET_TIMEOUT``
    (ten minutes) kills it — the user sees an opaque timeout instead of an
    authentication error. Both transports are pinned so they fail immediately
    and say why:

      * ``GIT_TERMINAL_PROMPT=0`` — HTTPS asks for username/password on the
        terminal. Credential HELPERS (osxkeychain, libsecret, gh's) are
        untouched, so a configured helper still authenticates silently. Forced,
        not defaulted: there is no terminal here to prompt on, so an inherited
        ``=1`` can only produce the hang.
      * ``GIT_SSH_COMMAND=ssh -o BatchMode=yes`` — SSH asks for a key
        passphrase or a host-key confirmation, and ssh reads those from
        ``/dev/tty`` directly, so redirecting stdin alone does not stop it.
        Only DEFAULTED: a user who set ``GIT_SSH_COMMAND`` (or the older
        ``GIT_SSH``) for a custom key or jump host keeps their own command
        verbatim.
    """
    out = dict(os.environ if env is None else env)
    out["GIT_TERMINAL_PROMPT"] = "0"
    if not out.get("GIT_SSH_COMMAND") and not out.get("GIT_SSH"):
        out["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return out


def _run(
    *args: str,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    check: bool = False,
    timeout: Optional[float] = _RUN_TIMEOUT,
) -> subprocess.CompletedProcess:
    try:
        cp = subprocess.run(
            list(args),
            cwd=cwd,
            env=_noninteractive_env(env),
            # No stdin for any provisioning command: nothing here is
            # interactive, and an inherited stdin is what lets a credential
            # prompt block instead of failing.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as err:
        # The child has been killed; surface the timeout as a failed run so
        # callers that branch on returncode keep working unchanged.
        out = err.output or b""
        msg = "timed out after {:g}s".format(timeout)
        cp = subprocess.CompletedProcess(
            list(args), 124, stdout=out + b"\n" + msg.encode("ascii"), stderr=None
        )
    if check and cp.returncode != 0:
        out = (cp.stdout or b"").decode("utf-8", "replace").strip()[-1000:]
        raise ProvisionError(
            "command failed ({}): {} -> {}".format(cp.returncode, " ".join(args), out)
        )
    return cp


def _resolve_base_sha(worktree_dir: str, base_branch: str) -> str:
    """Best diff base for a provisioned workspace: the fork point against
    ``base_branch`` (merge-base), falling back to the remote-tracking base, then
    the current HEAD. Returns "" only if even HEAD can't be resolved.
    """
    for args in (
        ["merge-base", "HEAD", base_branch],
        ["merge-base", "HEAD", "origin/" + base_branch],
        ["rev-parse", "HEAD"],
    ):
        try:
            cp = subprocess.run(
                ["git", "-C", worktree_dir, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            continue
        if cp.returncode == 0:
            sha = cp.stdout.decode("utf-8", "replace").strip()
            if sha:
                return sha
    return ""


# ---------------------------------------------------------------------------
# Base repo (worktree strategy)
# ---------------------------------------------------------------------------
def _check_branch_not_checked_out(base_repo: str, branch_name: str) -> None:
    """Raise a clear error if ``branch_name`` is already checked out in base_repo.

    `git worktree add` of a branch that is checked out in another live worktree
    fails with an opaque message; this turns it into an actionable one.
    """
    try:
        cp = subprocess.run(
            ["git", "-C", base_repo, "worktree", "list", "--porcelain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return
    if cp.returncode != 0:
        return
    target = "branch refs/heads/" + branch_name
    current = None
    for line in cp.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree ") :]
        elif line.strip() == target:
            raise ProvisionError(
                "branch '{}' is already checked out at {}. Kill that "
                "session first, or use a different story id / title.".format(
                    branch_name, current
                )
            )


def repo_display_name(repo_url: str) -> str:
    """The bare repo name behind ``repo_url`` — no owner, no host, no ``.git``.

    Transport-independent: every spelling of one repo yields one name, so
    ``git@github.com:Org/app.git``, ``https://github.com/Org/app`` and
    ``ssh://git@github.com:22/Org/app.git`` all give ``app``. That is what makes
    two spellings share ONE base clone (:func:`base_repo_dirname`) and show ONE
    label in the sidebar.

    Local clone sources have no forge behind them (``parse_remote`` returns
    ``None``), so they keep the historical tail-split: ``/home/me/app`` -> ``app``.
    """
    u = (repo_url or "").strip()
    ref = remote_url.parse_remote(u)
    if ref is not None:
        return ref.repo
    tail = u.rstrip("/").split("/")[-1]
    if not remote_url.is_local_path(u):
        # An scp-style remote whose path is a single segment
        # (``git@host:repo.git``) has no "/" in it at all, so the "/"-tail is
        # the WHOLE url — base clones and sidebar labels came out as
        # ``git@host:repo``. Everything up to the last ":" is ``user@host``.
        # Local paths are exempt: a Windows ``C:\repo`` would lose its drive
        # letter, and a POSIX path may legitimately contain ":".
        tail = tail.rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail


def base_repo_dirname(repo_url: str) -> str:
    """Per-repo base-clone dirname under the workspace dir: ``_base_<slug>``.

    Back-compat: the only spelling whose dirname CHANGES here is the
    single-segment scp remote (``git@host:repo.git``, formerly
    ``_base_git-host-repo``); the ``owner/repo`` spellings already agreed on
    their tail across transports.
    An affected base clone left over from the old naming is not migrated or
    deleted — the next provision simply clones a fresh ``_base_<repo>`` beside
    it. The orphan still matches :func:`is_base_repo_dirname`, so the workspace
    UI keeps classifying it as a base clone (not a stray session) and it can be
    removed by hand.
    """
    return BASE_REPO_PREFIX + slugify(repo_display_name(repo_url) or "repo")


def is_base_repo_dirname(name: str) -> bool:
    """Whether ``name`` is a canonical base clone dir."""
    return name.startswith(BASE_REPO_PREFIX)


def resolve_base_repo_dir(settings: ProvisionSettings) -> Path:
    """The base-clone directory for ``settings.repo_url``."""
    return settings.workspace_dir / base_repo_dirname(settings.repo_url)


def ensure_base_repo(settings: ProvisionSettings) -> str:
    """Ensure the canonical base clone for ``settings.repo_url`` exists and
    return its path.

    Worktree-strategy sessions are ``git worktree add``-ed off this repo. On
    first call it is cloned (full clone, so worktrees work) and checked out to
    ``base_branch``; on later calls it is fetched and fast-forwarded
    best-effort (failures are tolerated so an offline run still works).
    Resetting ``base_branch`` here never disturbs the per-session worktrees,
    which always live on their own ``feature/...`` branches.
    """
    base = resolve_base_repo_dir(settings)
    if (base / ".git").is_dir():
        _refresh_base_repo(base, settings)
    else:
        _clone_base_repo(base, settings)
    return str(base)


def _refresh_base_repo(base: Path, settings: ProvisionSettings) -> None:
    """Best-effort refresh of an existing canonical base clone to
    ``settings.base_branch``.

    Self-heals a base that got flipped to bare, fetches, and resets the base
    branch to its remote tip. Every step is tolerant: an offline run (or a
    ``base_branch`` that no longer resolves) leaves the base on its current HEAD
    rather than raising.
    """
    # Self-heal: the canonical base is always a normal (non-bare) working
    # clone. Worktree-add and the checkout/reset refresh below both need a
    # work tree, and `resolve_worktree_paths` resolves the repo via
    # `git rev-parse --show-toplevel`, which fails on a bare repo ("must be
    # run in a work tree"). If something flipped `core.bare` true out from
    # under us, restore it so provisioning doesn't wedge. Idempotent.
    _run("git", "-C", str(base), "config", "core.bare", "false")
    # Heal a base clone created before origin was split from the clone source:
    # its origin is still the user's local checkout, so it would fetch (and
    # push) there forever. Done BEFORE the fetch so this refresh already tracks
    # the forge.
    point_origin_at_forge(base, settings)
    # Refresh best-effort.
    _run(
        "git",
        "-C",
        str(base),
        "fetch",
        "origin",
        settings.base_branch,
        timeout=_NET_TIMEOUT,
    )
    co = _run(
        "git",
        "-C",
        str(base),
        "checkout",
        "-B",
        settings.base_branch,
        "origin/" + settings.base_branch,
    )
    if co.returncode == 0:
        _run(
            "git",
            "-C",
            str(base),
            "reset",
            "--hard",
            "origin/" + settings.base_branch,
        )
    else:
        _logger.info(
            "provision: base repo could not track %s; leaving on current HEAD",
            settings.base_branch,
        )


def _clone_base_repo(base: Path, settings: ProvisionSettings) -> None:
    """One-time clone of the canonical base repo into ``base``.

    Fast-paths a blobless, single-branch, tag-less clone of ``base_branch`` and
    falls back to a plain full clone (then a best-effort checkout) when the fast
    clone can't be used — e.g. ``base_branch`` isn't found or the server doesn't
    support partial clone.

    The blobless filter is applied ONLY to remote sources. A blobless clone
    records its source as a promisor remote and fetches missing blobs from it on
    demand — but a local clone source is about to be replaced as ``origin`` by
    the forge (:func:`point_origin_at_forge`), which would leave every deferred
    blob reachable only over the network, breaking history-dependent work
    offline. Cloning a local path in full costs nothing anyway: git hardlinks
    the object store, so the "full" clone is both faster and smaller on disk
    than the filtered one.
    """
    base.parent.mkdir(parents=True, exist_ok=True)
    _logger.info(
        "provision: cloning canonical base repo into %s (one-time setup)", base
    )
    # Fast path: a (for remote sources, blobless) single-branch, tag-less clone.
    # Worktrees still work and blobs are fetched on demand — dramatically faster
    # than a full clone of a large repo, which is what made the first worktree
    # session feel stuck.
    filter_args = (
        [] if remote_url.is_local_path(settings.repo_url) else ["--filter=blob:none"]
    )
    fast = _run(
        "git",
        "clone",
        *filter_args,
        "--no-tags",
        "--single-branch",
        "--branch",
        settings.base_branch,
        settings.repo_url,
        str(base),
        timeout=_NET_TIMEOUT,
    )
    if fast.returncode != 0:
        # Fallback: a plain clone (e.g. base_branch not found, or the server
        # doesn't support partial clone), then best-effort checkout.
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)
        _run(
            "git",
            "clone",
            settings.repo_url,
            str(base),
            check=True,
            timeout=_NET_TIMEOUT,
        )
        co = _run("git", "-C", str(base), "checkout", settings.base_branch)
        if co.returncode != 0:
            _logger.info(
                "provision: base_branch %s not found; base repo stays on default branch",
                settings.base_branch,
            )
    # The clone source may have been a local path; origin must be the forge, or
    # every session cut from this base pushes into the user's own checkout.
    point_origin_at_forge(base, settings)


# ---------------------------------------------------------------------------
# Provisioning (shared by both worktree strategies)
# ---------------------------------------------------------------------------
def provision_workspace(
    directory: str | Path,
    branch_name: str,
    settings: ProvisionSettings,
    *,
    seed_caches: bool = True,
) -> None:
    """Run the provisioning sequence against an already-created workspace.

    Setup commands (configured or auto-detected), cache-env pinning, cache
    seeding and git-excluding of mindflock's scratch artifacts. Each step is
    idempotent so it is safe to re-run on resume.
    """
    directory = Path(directory)
    _logger.info("provision: provisioning %s (branch %s)", directory, branch_name)

    workspace_setup.run_setup_commands(
        workspace_setup.resolve_setup_commands(settings.setup_commands, directory),
        directory,
        log_prefix="provision",
    )
    workspace_setup.pin_cache_env(directory, settings.caches)
    if seed_caches:
        workspace_setup.seed_caches(settings.caches, directory, log_prefix="provision")
    _install_precommit_log_wrapper(directory)
    workspace_setup.exclude_artifacts(directory)
    if settings.open_cursor:
        _launch_cursor(directory)
    _logger.info("provision: workspace ready at %s", directory)


def _launch_cursor(directory: Path) -> None:
    """Open the workspace in the configured IDE (Settings -> Advanced; Cursor by
    default). Best-effort, non-blocking, fire-and-forget."""
    from backend.config import ide as _ide
    from backend.web.core import ide_launch as _ide_launch

    try:
        _ide_launch.launch_ide(str(directory))
        _logger.info("provision: opened %s at %s", _ide.ide_name(), directory)
    except _ide_launch.IdeLaunchError as err:
        _logger.info("provision: skipping IDE launch — %s", err)
    except Exception as err:  # noqa: BLE001
        _logger.warning("provision: failed to launch %s: %s", _ide.ide_name(), err)


def _install_precommit_log_wrapper(directory: Path) -> None:
    script = directory / "auto_fix_precommit_hook.py"
    if not script.is_file():
        return
    # `uv run` may resolve/install an environment on first use — be generous.
    cp = _run("uv", "run", "python", script.name, cwd=str(directory), timeout=900)
    if cp.returncode != 0:
        _logger.warning(
            "provision: auto_fix_precommit_hook.py failed (continuing): %s",
            (cp.stdout or b"").decode("utf-8", "replace").strip()[-300:],
        )


# ---------------------------------------------------------------------------
# Prompt launcher
# ---------------------------------------------------------------------------
def _apply_binary_override(program: str) -> str:
    """Swap a program's executable for a user binary-path override.

    Given ``program`` (e.g. ``"aider --foo"`` or ``"/usr/bin/codex"``), resolve
    which provider claims it and look up that provider's binary override
    (settings ``coding_cli.binary_paths[name]`` / env
    ``MINDFLOCK_PROVIDER_BIN_<NAME>``). If an override is set, replace only the
    executable token (keeping any trailing args); otherwise return ``program``
    unchanged — so with no override the generated launcher is byte-identical to
    the pre-override behaviour.

    Best-effort: any resolution error leaves ``program`` untouched.
    """
    if not program:
        return program
    try:
        from backend import providers
        from backend.providers.config import binary_override

        parts = program.split()
        rest = parts[1:]
        prov = providers.resolve(program)
        override = binary_override(getattr(prov, "name", "") or "")
        if not override:
            return program
        return " ".join([override, *rest])
    except Exception:  # noqa: BLE001 — never break launch over an override lookup
        return program


def write_launcher(
    directory: str | Path,
    prompt: str,
    program: str = "claude",
    skip_permissions: bool = True,
    cache_env: Optional[dict] = None,
    launch_args=(),
) -> str:
    """Write the session launch script and return its absolute path.

    The script is what the tmux session runs (passed as a single argv element).
    It does two important things inside a ``bash -ilc`` login shell:

      1. ``export <KEY>=<value>`` for every cache env var (``cache_env``,
         defaulting to ``TESTMON_ENV=shared``) — this is the *authoritative*
         way a warm cache seed is kept valid. Every ``git commit`` the agent
         runs in this session inherits the exports, so e.g. the pre-commit
         ``pytest --testmon`` sees a stable env key regardless of where the
         active hook lives. This works even when the repo's hook framework sets
         ``core.hooksPath`` (which makes editing ``<gitdir>/hooks/pre-commit``
         directly ineffective) and without touching any tracked file.
      2. launch ``<program>``, seeded with ``prompt`` when one is given.

    With a prompt, the prompt is stored at ``.mindflock_prompt.md`` (so it
    survives restarts) and ``program`` is run with it; a trailing ``exec bash
    -i`` keeps the pane usable after the agent exits — the exported env carries
    across the ``exec``. Without a prompt (warm workspace), it just execs
    ``program``.

    Reboot-safe resume. The launcher distinguishes the *first* launch (where the
    ticket prompt should be seeded into a brand-new conversation) from every
    *re*-launch (where the prior conversation should be resumed instead). When
    WSL crashes the tmux server dies; on reboot ``backend.web._ensure_agent_session``
    re-runs this very script from scratch, and without this guard the ``first``
    branch would re-type the ticket prompt into a fresh conversation. A
    ``.mindflock_started`` marker (created on the first run) flips later runs to
    the ``--continue`` resume path.

    Exit handling. The in-session loop inspects the agent's exit code: a clean
    quit (0 / 130 = Ctrl-C) is treated as deliberate and drops to a shell rather
    than re-running the ticket; any other (crash / kill) resumes after a short
    pause.
    """
    # Absolute: the launcher's own path and its `cd` must work from tmux's
    # server cwd (not ours), so a relative worktree path would make the script
    # unfindable and the session exit immediately.
    directory = Path(os.path.abspath(directory))
    # --dangerously-skip-permissions also suppresses Claude's per-folder "Do you
    # trust the files in this folder?" gate, which would otherwise prompt on
    # every freshly-created worktree path.
    flag = " --dangerously-skip-permissions" if skip_permissions else ""

    program = program or "claude"
    if prompt:
        (directory / PROMPT_BASENAME).write_text(prompt, encoding="utf-8")
        seed = f' "$(cat {PROMPT_BASENAME})"'
    else:
        seed = ""

    # Cache env exports (default: the shared testmon key when no cache config
    # is in play).
    if cache_env is None:
        cache_env = {"TESTMON_ENV": TESTMON_ENV_NAME}
    exports = "".join(
        "export {}={}\n".format(k, _sh_quote(str(v)))
        for k, v in sorted(cache_env.items())
    )

    # Honor a user binary-path override for this provider (settings/env). When
    # no override is set this leaves ``program`` untouched, so the generated
    # script is byte-identical to the pre-override behaviour.
    launch_program = _apply_binary_override(program)
    if launch_args:
        import shlex

        launch_program = "%s %s" % (
            launch_program,
            " ".join(shlex.quote(str(a)) for a in launch_args),
        )
    prog = f"{launch_program}{flag}"
    first = f"{prog}{seed}"
    resume = _resume_chain(prog)

    # First launch (no marker) seeds the prompt into a fresh conversation.
    # A relaunch from scratch (marker present, e.g. a reboot after the tmux
    # server died = unnatural) resumes instead of re-typing the ticket; if the
    # resume fails twice it starts PLAIN — never re-seeded (see _resume_chain).
    #
    # In-session loop: after the agent exits, inspect its code. Clean quit
    # (0 = exit, 130 = Ctrl-C) is deliberate -> drop to a shell. Anything else
    # (crash / kill) -> resume after a 3s pause (Ctrl-C during the pause escapes
    # to a shell).
    inner = (
        f"{exports}"
        f"if [ -f {STARTED_MARKER} ]; then\n"
        f"  {resume}\n"
        "else\n"
        f"  : > {STARTED_MARKER}\n"
        f"  {first}\n"
        "fi\n"
        "while true; do\n"
        "  cs_code=$?\n"
        '  case "$cs_code" in 0|130) break;; esac\n'
        '  echo "[agent died (code $cs_code) — resuming in 3s; press Ctrl-C for a shell]"\n'
        "  sleep 3 || break\n"
        f"  {resume}\n"
        "done\n"
        "exec bash -i\n"
    )

    launch = directory / LAUNCHER_BASENAME
    # bash -ilc: login+interactive so PATH/aliases are present.
    script = (
        "#!/usr/bin/env bash\n"
        "cd " + _sh_quote(str(directory)) + "\n"
        "exec bash -ilc " + _sh_quote(inner) + "\n"
    )
    launch.write_text(script, encoding="utf-8")
    os.chmod(launch, 0o755)
    return str(launch)


def _resume_chain(prog: str) -> str:
    """The launcher's resume command: ``--continue``, one retry after a pause,
    then a PLAIN unseeded launch. The ticket prompt is deliberately NOT the
    fallback: a non-zero exit can't distinguish "nothing to continue" from a
    transient failure (network not up right after boot),
    and re-seeding while the conversation still exists silently restarts the
    whole ticket in a fresh thread — this mass-restarted tasks on 2026-07-09.
    The prompt survives at ``.mindflock_prompt.md`` for manual re-driving."""
    # Double quotes on the echo: the whole inner script is single-quote-wrapped
    # into `bash -ilc`, so single quotes here would turn into `'"'"'` soup.
    return (
        f"{prog} --continue || {{ sleep 3; {prog} --continue; }} || "
        '{ echo "[mindflock] resume failed twice; starting a fresh session'
        " WITHOUT re-sending the ticket prompt (kept in "
        f'{PROMPT_BASENAME})"; {prog}; }}'
    )


def _sh_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


# ---------------------------------------------------------------------------
# Worktree strategies (duck-type GitWorktree)
# ---------------------------------------------------------------------------
class ProvisionedWorktree(GitWorktree):
    """Worktree-strategy provisioned workspace: a git worktree off the base clone.

    Inherits the full ``GitWorktree`` lifecycle; only :meth:`Setup` is extended
    to run provisioning after the worktree is created. Because Resume calls
    Setup again (the worktree is removed on Pause), the workspace is correctly
    re-provisioned on resume.
    """

    def __init__(self, *, settings: Optional[ProvisionSettings], **kwargs) -> None:
        super().__init__(**kwargs)
        self._provision_settings = settings

    def Setup(self) -> None:  # noqa: N802 - mirror Go-cased API
        super().Setup()
        # setup_from_existing_branch (taken when the deterministic branch
        # already exists in the base repo) leaves baseCommitSHA empty,
        # which makes the Diff view run `git diff ''` and error. Backfill the
        # fork point so Diff has a valid comparison base.
        if not self.baseCommitSHA:
            base_branch = (
                self._provision_settings.base_branch
                if self._provision_settings is not None
                else "HEAD"
            )
            self.baseCommitSHA = _resolve_base_sha(self.worktreePath, base_branch)
        if self._provision_settings is not None:
            provision_workspace(
                self.worktreePath, self.branchName, self._provision_settings
            )
        else:
            _logger.warning(
                "provision: settings unavailable; skipping re-provision of %s",
                self.worktreePath,
            )

    setup = Setup


class ProvisionedCloneWorktree(GitWorktree):
    """Clone-strategy provisioned workspace: a full standalone ``git clone`` per
    session.

    Git-level operations (diff, commit, push, dirty-check) work natively
    because ``repoPath == worktreePath ==`` the clone. The clone is preserved
    across Pause (``Remove`` is a no-op) and only removed on Kill
    (``Cleanup``).
    """

    def __init__(self, *, settings: Optional[ProvisionSettings], **kwargs) -> None:
        super().__init__(**kwargs)
        self._provision_settings = settings

    def Setup(self) -> None:  # noqa: N802
        d = Path(self.worktreePath)
        if (d / ".git").exists():
            # Idempotent: clone already present (e.g. resume) — ensure the
            # branch is checked out, then re-provision (cheap/incremental).
            cp = _run("git", "-C", str(d), "checkout", self.branchName)
            if cp.returncode != 0:
                _run("git", "-C", str(d), "checkout", "-B", self.branchName)
        else:
            if self._provision_settings is None:
                raise ProvisionError(
                    "provisioning config unavailable; cannot re-clone " + str(d)
                )
            self._clone_and_branch(d)
        # Ensure a diff base — the adoption path (existing dir checked out) and
        # reloads leave baseCommitSHA empty, which would make Diff run `git diff
        # ''` and error.
        if not self.baseCommitSHA:
            base_branch = (
                self._provision_settings.base_branch
                if self._provision_settings is not None
                else "HEAD"
            )
            self.baseCommitSHA = _resolve_base_sha(str(d), base_branch)
        if self._provision_settings is not None:
            provision_workspace(str(d), self.branchName, self._provision_settings)
        else:
            _logger.warning(
                "provision: settings unavailable; skipping re-provision of %s", d
            )

    def _clone_and_branch(self, d: Path) -> None:
        d.parent.mkdir(parents=True, exist_ok=True)
        # Clone into a sibling temp dir first to avoid races with background
        # processes recreating `d` mid-clone (mirrors the pipeline's clone-tmp
        # dance).
        tmp = d.with_name(d.name + ".clone-tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        settings = self._provision_settings
        # Try to fork from base_branch (aligns with any cache seed); fall back
        # to a plain shallow clone of the default branch.
        cp = _run(
            "git",
            "clone",
            "--depth=1",
            "--branch",
            settings.base_branch,
            settings.repo_url,
            str(tmp),
            timeout=_NET_TIMEOUT,
        )
        if cp.returncode != 0:
            cp = _run(
                "git",
                "clone",
                "--depth=1",
                settings.repo_url,
                str(tmp),
                check=True,
                timeout=_NET_TIMEOUT,
            )
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        tmp.rename(d)
        # Same split as the worktree strategy: clone from wherever is fastest,
        # then push to the forge.
        point_origin_at_forge(d, settings)
        # Record the base commit so the diff view has a comparison point.
        head = _run("git", "-C", str(d), "rev-parse", "HEAD")
        if head.returncode == 0:
            self.baseCommitSHA = head.stdout.decode("utf-8", "replace").strip()
        _run("git", "-C", str(d), "checkout", "-B", self.branchName)

    def Remove(self) -> None:  # noqa: N802
        # Keep the clone on disk across Pause (there is no shared repo to detach
        # from; the branch lives inside this clone).
        return None

    def Prune(self) -> None:  # noqa: N802
        return None

    def IsBranchCheckedOut(self) -> bool:  # noqa: N802
        # The clone is always "on" its branch; Resume's guard does not apply.
        return False

    def Cleanup(self) -> None:  # noqa: N802
        shutil.rmtree(self.worktreePath, ignore_errors=True)

    def keeps_dir_across_pause(self) -> bool:
        # Signals Instance.Pause not to rmtree this directory (even in the
        # orphan fast-path): the branch + committed work live in this clone.
        return True

    setup = Setup
    remove = Remove
    prune = Prune
    is_branch_checked_out = IsBranchCheckedOut
    cleanup = Cleanup


def build_provisioned_worktree(
    strategy: str,
    branch_name: str,
    session_name: str,
    settings: ProvisionSettings,
    workspace_path: Optional[str] = None,
) -> GitWorktree:
    """Construct the right provisioned worktree for ``strategy``
    (``'worktree'``|``'clone'``).

    Returns a ``GitWorktree``-compatible object with ``repoPath`` / ``branchName``
    / ``worktreePath`` populated and ready for ``Setup()``.

    ``workspace_path`` adopts an already-provisioned directory with
    ``branch_name`` already checked out (e.g. a PR workspace created by
    ``PRProvisioner``). The branch is used verbatim (not sanitized — it is a real
    existing ref) and treated clone-style: a standalone repo with an idempotent
    Setup that just re-checks-out the branch and re-provisions.
    """
    if workspace_path is not None:
        return ProvisionedCloneWorktree(
            settings=settings,
            repoPath=workspace_path,
            worktreePath=workspace_path,
            sessionName=session_name,
            branchName=branch_name,
        )

    branch_name = sanitize_branch_name(branch_name) or branch_name
    if strategy == "clone":
        worktree_path = str(settings.workspace_dir / branch_name.replace("/", "-"))
        return ProvisionedCloneWorktree(
            settings=settings,
            repoPath=worktree_path,
            worktreePath=worktree_path,
            sessionName=session_name,
            branchName=branch_name,
        )

    # Default: worktree strategy off the canonical base clone.
    base_repo = ensure_base_repo(settings)
    # Worktree-strategy branch names are deterministic and the base repo is
    # shared, so a second live session on the same branch would otherwise fail
    # deep in `git worktree add` with an opaque error. Surface it clearly up
    # front.
    _check_branch_not_checked_out(base_repo, branch_name)
    resolved_repo, worktree_path = resolve_worktree_paths(base_repo, branch_name)
    return ProvisionedWorktree(
        settings=settings,
        repoPath=resolved_repo,
        worktreePath=worktree_path,
        sessionName=session_name,
        branchName=branch_name,
    )
