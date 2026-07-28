"""``mindflock uninstall`` — undo everything MindFlock wrote outside its own venv.

Removing the engine is not just ``uv tool uninstall mindflock``. That deletes
the tool venv and the ``~/.local/bin/mindflock`` shim, but MindFlock also
writes into places that outlive it, and two of them cause real trouble if
they're left behind:

**Worktrees.** ``~/.mindflock/worktrees/<…>`` are *live git worktrees*
registered inside the user's own repositories, along with the session branches
that back them. Deleting the engine (or ``rm -rf``-ing the directory) strands
both: ``git worktree list`` in the user's repo keeps pointing at paths that no
longer exist, and nothing is left on disk that knows how to clean them up.

**Activity hooks.** For in-place sessions MindFlock merges hook entries into
the *user's own repo* — ``.claude/settings.local.json``, ``.codex/hooks.json``
(see :func:`backend.providers.activity_markers.merge_activity_hooks`). The hook
body is self-contained inline ``python3``: it has no dependency on the
``mindflock`` binary, so it keeps firing on every agent tool call after the
engine is gone, and its ``os.makedirs`` re-creates
``~/.mindflock-assistant/.activity-markers`` — meaning a "clean" uninstall
silently regrows the directory the user just deleted.

So this module walks every workdir recorded in ``state.json`` and reverses
those writes, then (only under ``--purge``) removes MindFlock's two home
directories.

Deliberately conservative:

* It never deletes a user directory — only MindFlock's own scratch files
  *inside* one (:data:`backend.workspace_setup.WORKSPACE_ARTIFACTS`).
* A worktree is only removed when it lives under ``~/.mindflock/worktrees``.
  Anything else (in-place sessions, or a worktree the user relocated) is left
  alone and reported, because MindFlock did not create that directory.
* Hook removal is tag-scoped: user-authored hooks in the same file survive.
* ``--purge`` is opt-in; by default usage history and settings stay, so a
  reinstall picks up where the user left off.
* It refuses to run while a server is up — tearing down worktrees under a live
  session would leave the engine writing into deleted directories.

The final ``uv tool uninstall mindflock`` is *printed*, not run: this process
is executing out of the very venv that command deletes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

__all__ = ["Plan", "Report", "build_plan", "execute", "home_dirs"]

#: Timeout for every git call we make. Uninstall must not hang on a wedged repo.
_GIT_TIMEOUT_S = 60

#: MindFlock's two home directories, removed only under ``--purge``.
#: ``~/.mindflock`` holds state/settings/worktrees; ``~/.mindflock-assistant``
#: holds usage history, pricing and the activity/thread markers.
_HOME_DIR_NAMES = (".mindflock", ".mindflock-assistant")


def home_dirs() -> List[str]:
    """Absolute paths of MindFlock's home directories that currently exist."""
    home = os.path.expanduser("~")
    return [
        p for p in (os.path.join(home, n) for n in _HOME_DIR_NAMES) if os.path.isdir(p)
    ]


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
@dataclass
class SessionTarget:
    """One session's cleanup target, derived from a ``state.json`` instance."""

    title: str
    repo_path: str
    worktree_path: str
    branch: str = ""
    is_existing_branch: bool = False
    in_place: bool = False

    @property
    def removable_worktree(self) -> bool:
        """True when the worktree is MindFlock-created and safe to remove.

        Requires all of: not an in-place session, a worktree path distinct from
        the repo, and a path under ``~/.mindflock/worktrees``. The last
        condition is the load-bearing one — it is what keeps this from ever
        running ``git worktree remove`` against a directory the user made.
        """
        if self.in_place or not self.worktree_path or not self.repo_path:
            return False
        if os.path.realpath(self.worktree_path) == os.path.realpath(self.repo_path):
            return False
        return _under_worktree_dir(self.worktree_path)


@dataclass
class Plan:
    """What :func:`execute` will do. Rendered verbatim by ``--dry-run``."""

    sessions: List[SessionTarget] = field(default_factory=list)
    #: Directories to strip hooks / scratch artifacts / exclude lines from.
    workdirs: List[str] = field(default_factory=list)
    #: Orphaned directories under ~/.mindflock/worktrees with no live session.
    orphan_worktrees: List[str] = field(default_factory=list)
    #: Home directories --purge would remove.
    purge_dirs: List[str] = field(default_factory=list)
    #: Non-fatal problems found while planning (unreadable state, etc.).
    warnings: List[str] = field(default_factory=list)


@dataclass
class Report:
    """What :func:`execute` actually did — one human-readable line per action."""

    actions: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def did(self, line: str) -> None:
        self.actions.append(line)

    def failed(self, line: str) -> None:
        self.errors.append(line)


def _under_worktree_dir(path: str) -> bool:
    """True when ``path`` sits inside ``~/.mindflock/worktrees``."""
    try:
        from backend.session.git.worktree import get_worktree_directory

        root = os.path.realpath(get_worktree_directory())
    except Exception:  # noqa: BLE001 — no config dir resolvable
        return False
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)


def _load_instances() -> tuple[list, Optional[str]]:
    """Instances recorded in ``state.json`` as plain dicts, plus a warning.

    Reads the file directly rather than going through ``LoadState``: uninstall
    must work even when the state file is a schema this build refuses to parse,
    and LoadState would move such a file aside as a side effect.
    """
    import json

    try:
        from backend.config.config import GetConfigDir

        path = os.path.join(GetConfigDir(), "state.json")
    except OSError as err:
        return [], "could not locate the config directory: %s" % err
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read() or b"{}")
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as err:
        return [], (
            "could not read %s (%s) — worktrees and hooks recorded there "
            "cannot be cleaned up automatically" % (path, err)
        )
    instances = doc.get("instances") if isinstance(doc, dict) else None
    return (instances if isinstance(instances, list) else []), None


def _target_from_instance(inst: dict) -> SessionTarget:
    wt = inst.get("worktree") if isinstance(inst.get("worktree"), dict) else {}
    repo = str(wt.get("repo_path") or inst.get("path") or "")
    return SessionTarget(
        title=str(inst.get("title") or ""),
        repo_path=repo,
        worktree_path=str(wt.get("worktree_path") or inst.get("path") or ""),
        branch=str(wt.get("branch_name") or inst.get("branch") or ""),
        is_existing_branch=bool(wt.get("is_existing_branch")),
        in_place=bool(inst.get("in_place")),
    )


def _orphan_worktrees(known: set) -> tuple[List[str], Optional[str]]:
    """Directories under ``~/.mindflock/worktrees`` no live session claims.

    These accumulate when a session is force-deleted or the state file is lost;
    without this sweep they'd survive a --purge-less uninstall as registered
    worktrees in repos we never visit.

    Returns ``(paths, warning)``. The warning matters: an empty list means
    "nothing to clean" to the reader, so a scan that *failed* must say so
    rather than silently reporting zero. Absence of the directory is the one
    genuinely-nothing-to-do case and warns nothing.
    """
    try:
        from backend.session.git.worktree import get_worktree_directory

        root = get_worktree_directory()
    except Exception as err:  # noqa: BLE001 — config dir unresolvable
        return [], "could not locate the worktrees directory (%s)" % err
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except FileNotFoundError:
        return [], None  # no worktrees dir: genuinely nothing to sweep
    except OSError as err:
        return [], "could not scan %s (%s) — orphaned worktrees not checked" % (
            root,
            err,
        )
    out = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if os.path.realpath(entry.path) not in known:
            out.append(entry.path)
    return out, None


def build_plan() -> Plan:
    """Survey the machine and return everything uninstall would touch."""
    plan = Plan()
    instances, warning = _load_instances()
    if warning:
        plan.warnings.append(warning)

    seen_workdirs: set = set()
    known_worktrees: set = set()
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        target = _target_from_instance(inst)
        plan.sessions.append(target)
        if target.worktree_path:
            known_worktrees.add(os.path.realpath(target.worktree_path))
        # Both ends matter: hooks land in the workdir the agent runs in (the
        # worktree), and for in-place sessions that IS the user's repo.
        for d in (target.repo_path, target.worktree_path):
            if not d or not os.path.isdir(d):
                continue
            real = os.path.realpath(d)
            if real in seen_workdirs:
                continue
            seen_workdirs.add(real)
            plan.workdirs.append(d)

    plan.orphan_worktrees, orphan_warning = _orphan_worktrees(known_worktrees)
    if orphan_warning:
        plan.warnings.append(orphan_warning)
    plan.purge_dirs = home_dirs()
    return plan


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def _hook_files(workdir: str) -> List[str]:
    """Relative paths of every hooks config MindFlock may have written here.

    Claude's path is hardcoded in its provider; every config-driven provider
    declares its own via ``ProviderConfig.activity_hooks_file``. Reading the
    live registry means a user-defined provider's hooks file is cleaned up too.
    """
    rels = [".claude/settings.local.json"]
    try:
        from backend import providers

        for prov in providers.all_providers():
            cfg = getattr(prov, "cfg", None)
            rel = getattr(cfg, "activity_hooks_file", "") if cfg else ""
            if rel and rel not in rels:
                rels.append(rel)
    except Exception:  # noqa: BLE001 — registry unavailable; the default still applies
        pass
    return rels


def clean_workdir(workdir: str, report: Report, dry_run: bool = False) -> None:
    """Reverse every write MindFlock made *inside* ``workdir``.

    Strips tagged hook entries from each provider's hooks file, deletes the
    scratch artifacts, and drops the matching ``.git/info/exclude`` lines. The
    directory itself is never removed.
    """
    from backend.providers import activity_markers
    from backend.workspace_setup import WORKSPACE_ARTIFACTS

    for rel in _hook_files(workdir):
        path = os.path.join(workdir, rel)
        if not os.path.isfile(path):
            continue
        if dry_run:
            # Peek without writing so --dry-run doesn't over-report files that
            # only contain the user's own hooks.
            if _has_mindflock_hooks(path):
                report.did("would strip MindFlock hooks from %s" % path)
            continue
        if activity_markers.remove_activity_hooks(path):
            report.did("stripped MindFlock hooks from %s" % path)
        if activity_markers.remove_git_exclude(workdir, rel):
            report.did("removed %s from %s/.git/info/exclude" % (rel, workdir))

    for artifact in WORKSPACE_ARTIFACTS:
        path = os.path.join(workdir, artifact.rstrip("/"))
        is_dir = artifact.endswith("/")
        if not (os.path.isdir(path) if is_dir else os.path.isfile(path)):
            continue
        if dry_run:
            report.did("would remove %s" % path)
            continue
        try:
            if is_dir:
                shutil.rmtree(path)
            else:
                os.remove(path)
            report.did("removed %s" % path)
        except OSError as err:
            report.failed("could not remove %s: %s" % (path, err))
        if activity_markers.remove_git_exclude(workdir, artifact):
            report.did("removed %s from %s/.git/info/exclude" % (artifact, workdir))


def _has_mindflock_hooks(path: str) -> bool:
    """True when ``path`` holds at least one tagged MindFlock hook entry."""
    import json

    from backend.providers.activity_markers import is_mindflock_hook_entry

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            return False
        return any(
            is_mindflock_hook_entry(e)
            for entries in hooks.values()
            if isinstance(entries, list)
            for e in entries
        )
    except (OSError, ValueError, AttributeError):
        return False


def _git(repo: str, *args: str) -> tuple[int, str]:
    """Run a git command in ``repo``; return ``(returncode, combined output)``."""
    try:
        cp = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return 1, str(err)
    return cp.returncode, ((cp.stdout or "") + (cp.stderr or "")).strip()


def remove_session_worktree(
    target: SessionTarget, report: Report, dry_run: bool = False
) -> None:
    """Remove one session's worktree + branch, then prune the repo.

    This is the step that keeps the user's repo consistent: without the prune,
    the repo is left with a registered worktree pointing at a deleted path.
    """
    if not target.removable_worktree:
        if target.worktree_path and not target.in_place:
            report.did(
                "left worktree %s alone (outside ~/.mindflock/worktrees — "
                "MindFlock did not create it)" % target.worktree_path
            )
        return
    if dry_run:
        report.did(
            "would remove worktree %s and prune %s"
            % (target.worktree_path, target.repo_path)
        )
        if target.branch and not target.is_existing_branch:
            report.did(
                "would delete branch %s in %s" % (target.branch, target.repo_path)
            )
        return

    if os.path.exists(target.worktree_path):
        code, out = _git(
            target.repo_path, "worktree", "remove", "-f", target.worktree_path
        )
        if code == 0:
            report.did("removed worktree %s" % target.worktree_path)
        else:
            # git refuses when the repo is gone or the worktree is corrupt;
            # fall back to deleting the directory so the prune below can
            # deregister it.
            report.failed(
                "git worktree remove failed for %s (%s) — deleting the directory"
                % (target.worktree_path, out.splitlines()[0] if out else "no output")
            )
            shutil.rmtree(target.worktree_path, ignore_errors=True)

    # A session branch MindFlock created has no value once its worktree is
    # gone; one that pre-existed the session belongs to the user.
    if target.branch and not target.is_existing_branch:
        code, out = _git(target.repo_path, "branch", "-D", target.branch)
        if code == 0:
            report.did("deleted branch %s in %s" % (target.branch, target.repo_path))
        elif "not found" not in out:
            report.failed("could not delete branch %s: %s" % (target.branch, out))

    code, out = _git(target.repo_path, "worktree", "prune")
    if code == 0:
        report.did("pruned stale worktree registrations in %s" % target.repo_path)
    else:
        report.failed("git worktree prune failed in %s: %s" % (target.repo_path, out))


def _purge_dir(path: str, report: Report, dry_run: bool = False) -> None:
    """``rm -rf`` one of MindFlock's home directories, with a sanity guard."""
    home = os.path.realpath(os.path.expanduser("~"))
    real = os.path.realpath(path)
    # Refuse anything that isn't literally ~/.mindflock[-assistant]: this is the
    # only rm -rf in the uninstaller and it must not be steerable by a weird
    # $HOME or a symlinked config dir.
    if os.path.dirname(real) != home or os.path.basename(real) not in _HOME_DIR_NAMES:
        report.failed("refused to purge %s (not a MindFlock home directory)" % path)
        return
    if dry_run:
        report.did("would delete %s" % path)
        return
    try:
        shutil.rmtree(real)
        report.did("deleted %s" % path)
    except OSError as err:
        report.failed("could not delete %s: %s" % (path, err))


def server_is_running(host: Optional[str] = None, port: Optional[int] = None) -> bool:
    """True when a MindFlock server answers on the configured host/port.

    Uninstalling under a live server would pull worktrees out from under
    running sessions, so the CLI treats this as a hard stop.
    """
    try:
        from backend import client

        client.discover(host, port)
        return True
    except Exception:  # noqa: BLE001 — ServerNotFound and any probe failure alike
        return False


def execute(
    plan: Plan,
    purge: bool = False,
    dry_run: bool = False,
    keep_worktrees: bool = False,
) -> Report:
    """Carry out ``plan``. Returns a :class:`Report` of what happened.

    Order matters: worktrees are torn down through git *before* anything is
    deleted wholesale, so the repos they're registered in stay consistent.
    """
    report = Report()

    if not keep_worktrees:
        for target in plan.sessions:
            remove_session_worktree(target, report, dry_run=dry_run)

    for workdir in plan.workdirs:
        clean_workdir(workdir, report, dry_run=dry_run)

    for orphan in plan.orphan_worktrees:
        if keep_worktrees:
            break
        if dry_run:
            report.did("would delete orphaned worktree directory %s" % orphan)
            continue
        try:
            shutil.rmtree(orphan)
            report.did("deleted orphaned worktree directory %s" % orphan)
        except OSError as err:
            report.failed("could not delete %s: %s" % (orphan, err))

    if purge:
        for path in plan.purge_dirs:
            _purge_dir(path, report, dry_run=dry_run)

    return report
