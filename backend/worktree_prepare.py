"""Prepare a new session worktree: carry pin files, reuse or install deps.

A fresh ``git worktree`` has only what git tracks. Two things are missing
before an agent can run anything:

* **untracked pin files** — ``.python-version`` (without it ``uv`` builds the
  wrong interpreter), ``.nvmrc``, ``.tool-versions`` … :func:`carry_files`
  copies the well-known ones (:data:`DEFAULT_CARRY`) from the source checkout.
  Never a secret by default: :func:`looks_secret` refuses ``.env*``, keys and
  credential files unless the user lists them explicitly.
* **installed dependencies** — :func:`plan_dependencies` finds the tracked
  lockfiles and :func:`run_dependency_step` makes each ecosystem ready the
  cheapest *correct* way:

  ======  ============================================================
  uv      ``uv sync --frozen``. uv links every file from its own cache
          (hard links on Linux, clones on APFS), so a venv costs almost
          nothing on disk and seconds to build. A venv is never copied:
          it embeds absolute paths (shebangs, the editable install of the
          project points at the SOURCE checkout).
  npm     the source checkout's ``node_modules`` cloned when its lockfile
  yarn 1  is byte-identical (and npm/yarn recorded a finished install):
          copy-on-write where the filesystem can (``cp --reflink`` on
          btrfs/XFS, ``clonefile`` on APFS), else a HARD-LINK tree. Both
          package managers replace a package's files rather than edit them
          in place, so a later ``npm install`` in the worktree never writes
          through a shared inode; the few files they DO rewrite in place
          (``node_modules/.package-lock.json``, ``.yarn-integrity``) are
          copied, not linked, and tool caches (``.cache``, ``.vite``) are
          left behind. Otherwise ``npm ci`` / ``yarn install`` from the
          local cache. Never a symlink: the bundler writes a symlinked
          ``node_modules``' real path into its output (stale bundle in CI).
  pnpm    ``pnpm install --frozen-lockfile --prefer-offline`` — pnpm's own
          store already hard-links every package.
  ======  ============================================================

``auto`` mirrors the source checkout: only an ecosystem the source has
installed is made ready (:func:`installed_in`). ``install`` installs every
lockfile's dependencies with the package manager, ``off`` none.

Removing a worktree only unlinks its names: a hard-linked file lives on while
any other worktree (or the source) links it, so nothing here can delete data
another checkout needs. The runner (threads, status, log, events) is
:mod:`backend.web.core.worktree_setup`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Dict, List, Optional, Sequence

__all__ = [
    "DEFAULT_CARRY",
    "DEPENDENCY_MODES",
    "DEPS_ENV",
    "DepStep",
    "looks_secret",
    "carry_files",
    "plan_dependencies",
    "run_dependency_step",
    "clone_tree",
    "deps_disabled",
]

#: Untracked files carried into every new worktree when the source has them:
#: tool-version pins only. Small, never secret, and without them the worktree
#: resolves a different toolchain than the checkout it came from.
DEFAULT_CARRY = (
    ".python-version",
    ".nvmrc",
    ".node-version",
    ".tool-versions",
    ".ruby-version",
    ".java-version",
    ".go-version",
    ".terraform-version",
    ".sdkmanrc",
)

#: Biggest default-carried file: a pin is a line or two; anything larger is
#: not what the list means.
CARRY_MAX_BYTES = 64 * 1024

#: Names never carried by DEFAULT (a user may still list one explicitly).
#: ``.envrc`` is here too: direnv files routinely export tokens.
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*credential*",
    "*secret*",
    "*token*",
)

#: ``[workspace].dependencies``: ``auto`` (what the source checkout has
#: installed: reused when the lockfile matches, else installed), ``install``
#: (every lockfile, always through the package manager), ``off``.
DEPENDENCY_MODES = ("auto", "install", "off")

#: Machine-wide off switch for the dependency step (``0``); the test suite
#: sets it so a session created in a fixture repo never installs anything.
DEPS_ENV = "MINDFLOCK_WORKTREE_DEPS"

#: Lockfile -> package manager, in planning order.
_LOCKFILES = (
    ("uv.lock", "uv"),
    ("pnpm-lock.yaml", "pnpm"),
    ("package-lock.json", "npm"),
    ("yarn.lock", "yarn"),
)
#: At most this many dependency steps (a monorepo of 40 packages with their
#: own lockfiles is configured by hand, not guessed at).
MAX_STEPS = 6
#: Deepest lockfile considered (``frontend/package-lock.json`` is depth 1).
MAX_DEPTH = 2

#: node_modules entries never shared: tool caches written in place.
_NM_SKIP = (".cache", ".vite", ".vite-temp", ".parcel-cache", ".eslintcache")
#: node_modules files the package manager itself rewrites in place: copied.
_NM_COPY = (".package-lock.json", ".yarn-integrity", ".modules.yaml")
#: Proof a node_modules is a finished install, per manager.
_NM_MARKER = {"npm": ".package-lock.json", "yarn": ".yarn-integrity"}


def looks_secret(rel: str) -> bool:
    """Whether a path's name looks like it holds a secret."""
    name = os.path.basename(rel.rstrip("/")).lower()
    return any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS)


def deps_disabled() -> bool:
    return os.environ.get(DEPS_ENV, "").strip().lower() in ("0", "false", "off", "no")


# --- carry -----------------------------------------------------------------
def _inside(root: Path, p: Path) -> bool:
    return str(p) == str(root) or str(p).startswith(str(root) + os.sep)


def carry_files(
    source: str, wt: str, explicit: Sequence[str] = (), defaults: bool = True
) -> List[str]:
    """Copy untracked files from ``source`` into ``wt``; returns what was copied.

    ``defaults`` carries :data:`DEFAULT_CARRY` — regular files only (no
    symlink, nothing over :data:`CARRY_MAX_BYTES`, nothing that
    :func:`looks_secret`). ``explicit`` entries are the user's or the repo's
    own list and are copied as named (files or folders, ``.env`` included).
    Paths are repo-relative; an escape (``..``, absolute) is refused. Never
    overwrites anything already in the worktree, never raises."""
    copied: List[str] = []
    try:
        src_root = Path(source).resolve()
        wt_root = Path(wt).resolve()
    except (OSError, RuntimeError):
        return copied
    if not src_root.is_dir() or not wt_root.is_dir() or src_root == wt_root:
        return copied
    wanted: List[tuple] = []
    if defaults:
        wanted += [(rel, False) for rel in DEFAULT_CARRY]
    wanted += [(rel, True) for rel in explicit if rel and rel.strip()]
    seen = set()
    for rel, is_explicit in wanted:
        rel = rel.strip()
        if rel in seen:
            continue
        seen.add(rel)
        try:
            if os.path.isabs(rel) or ".." in Path(rel).parts:
                continue
            src = src_root / rel
            dst = wt_root / rel
            if not _inside(src_root, src.resolve()) or os.path.lexists(dst):
                continue
            if not is_explicit:
                if looks_secret(rel) or src.is_symlink() or not src.is_file():
                    continue
                if src.stat().st_size > CARRY_MAX_BYTES:
                    continue
            if not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
            copied.append(rel)
        except Exception:  # noqa: BLE001 — best-effort per entry
            continue
    return copied


# --- plan ------------------------------------------------------------------
@dataclass
class DepStep:
    """One ecosystem to make ready: ``manager`` in ``dir`` (repo-relative,
    ``""`` = the root), keyed by its ``lockfile``."""

    manager: str
    dir: str
    lockfile: str

    @property
    def name(self) -> str:
        return "%s (%s)" % (self.manager, self.dir or ".")

    def to_dict(self) -> dict:
        return {"manager": self.manager, "dir": self.dir, "lockfile": self.lockfile}


def installed_in(checkout: str, step: DepStep) -> bool:
    """Whether ``checkout`` has ``step``'s dependencies installed: a venv
    (``.venv/pyvenv.cfg``) for uv, a ``node_modules`` folder for the node
    managers. ``auto`` mirrors the source checkout: an ecosystem the user
    never installed there (this repo's ``electron/``, say) is not installed
    in every worktree either."""
    here = os.path.join(checkout, step.dir)
    if step.manager == "uv":
        return os.path.isfile(os.path.join(here, ".venv", "pyvenv.cfg"))
    return os.path.isdir(os.path.join(here, "node_modules"))


def plan_dependencies(checkout: str, source: str = "") -> List[DepStep]:
    """The dependency steps ``checkout``'s TRACKED lockfiles call for.

    One step per directory (a dir with ``pnpm-lock.yaml`` and a stray
    ``package-lock.json`` is pnpm's), uv needs a ``pyproject.toml`` beside its
    lock and the node managers a ``package.json``. At most :data:`MAX_STEPS`,
    no deeper than :data:`MAX_DEPTH`. With ``source``, only the steps the
    source checkout has installed (:func:`installed_in`). ``[]`` when git
    can't list the files."""
    try:
        cp = subprocess.run(
            ["git", "-C", checkout, "ls-files", "-z", "--"]
            + [":(glob)**/" + name for name, _m in _LOCKFILES],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if cp.returncode != 0:
        return []
    files = [f for f in cp.stdout.decode("utf-8", "replace").split("\0") if f]
    by_dir: Dict[str, set] = {}
    for f in files:
        d, _sep, base = f.rpartition("/")
        if d.count("/") + (1 if d else 0) > MAX_DEPTH:
            continue
        if "node_modules" in d.split("/"):
            continue
        by_dir.setdefault(d, set()).add(base)
    steps: List[DepStep] = []
    node_done = set()
    for name, manager in _LOCKFILES:
        for d in sorted(by_dir):
            if name not in by_dir[d]:
                continue
            here = os.path.join(checkout, d)
            if manager == "uv":
                if not os.path.isfile(os.path.join(here, "pyproject.toml")):
                    continue
            else:
                if d in node_done or not os.path.isfile(
                    os.path.join(here, "package.json")
                ):
                    continue
                node_done.add(d)
            step = DepStep(manager, d, name)
            if source and not installed_in(source, step):
                continue
            steps.append(step)
    return steps[:MAX_STEPS]


# --- tree cloning --------------------------------------------------------------
def _gnu_cp() -> bool:
    if os.name == "nt" or not shutil.which("cp"):
        return False
    try:
        cp = subprocess.run(
            ["cp", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0 and b"GNU" in cp.stdout


def _cp(argv: List[str], timeout: float) -> bool:
    try:
        cp = subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0


def _can_reflink(src_dir: str, dst_dir: str) -> bool:
    """Whether a copy-on-write clone works from ``src_dir``'s filesystem into
    ``dst_dir`` (GNU ``cp --reflink=always`` on one small probe file). ext4
    answers no ("Operation not supported"); btrfs/XFS(reflink=1) yes."""
    probe_dst = os.path.join(dst_dir, ".mindflock-reflink-probe")
    # The probe must start on the source's filesystem, and the source is never
    # written: clone a file already in it.
    candidate = ""
    try:
        with os.scandir(src_dir) as it:
            for e in it:
                if e.is_file(follow_symlinks=False):
                    candidate = e.path
                    break
    except OSError:
        return False
    if not candidate:
        return False
    try:
        return _cp(["cp", "--reflink=always", candidate, probe_dst], 10)
    finally:
        try:
            os.unlink(probe_dst)
        except OSError:
            pass


def _link_tree_py(src: str, dst: str) -> None:
    """A hard-link tree of ``src`` at ``dst`` (dirs made, files linked,
    symlinks recreated as they are). Raises ``OSError``."""
    os.makedirs(dst)
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out = dst if rel == "." else os.path.join(dst, rel)
        for d in list(dirs):
            s = os.path.join(root, d)
            if os.path.islink(s):
                os.symlink(os.readlink(s), os.path.join(out, d))
                dirs.remove(d)
            else:
                os.mkdir(os.path.join(out, d))
        for f in files:
            s = os.path.join(root, f)
            if os.path.islink(s):
                os.symlink(os.readlink(s), os.path.join(out, f))
            else:
                os.link(s, os.path.join(out, f))


def clone_tree(src: str, dst: str, timeout: float = 600) -> str:
    """Clone directory ``src`` to ``dst`` (which must not exist) as cheaply
    as the filesystem allows; returns the method: ``reflink`` (copy-on-write,
    GNU cp), ``clonefile`` (APFS, macOS cp -c) or ``hardlink``. Builds into a
    temp name and renames, so ``dst`` is whole or absent. Raises
    ``OSError`` when nothing worked."""
    parent = os.path.dirname(os.path.abspath(dst))
    tmp = dst + ".mindflock-tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    method = ""
    if os.name != "nt":
        if sys.platform == "darwin":
            if _cp(["cp", "-c", "-R", "-p", src, tmp], timeout):
                method = "clonefile"
            else:
                shutil.rmtree(tmp, ignore_errors=True)
        elif _gnu_cp():
            if _can_reflink(src, parent) and _cp(
                ["cp", "-a", "--reflink=always", src, tmp], timeout
            ):
                method = "reflink"
            else:
                shutil.rmtree(tmp, ignore_errors=True)
                if _cp(["cp", "-al", src, tmp], timeout):
                    method = "hardlink"
                else:
                    shutil.rmtree(tmp, ignore_errors=True)
    if not method:
        try:
            _link_tree_py(src, tmp)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        method = "hardlink"
    os.rename(tmp, dst)
    return method


def _unshare_node_modules(nm: str) -> None:
    """After a hard-link clone: drop tool caches and turn the files the package
    manager rewrites in place into private copies (see the module doc)."""
    for name in _NM_SKIP:
        p = os.path.join(nm, name)
        if os.path.islink(p) or os.path.isfile(p):
            os.unlink(p)
        elif os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    for name in _NM_COPY:
        p = os.path.join(nm, name)
        if os.path.isfile(p) and not os.path.islink(p):
            tmp = p + ".mindflock-copy"
            shutil.copy2(p, tmp)
            os.replace(tmp, p)


# --- running a step ------------------------------------------------------------
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _git_ignored(checkout: str, rel: str) -> bool:
    try:
        cp = subprocess.run(
            ["git", "-C", checkout, "check-ignore", "-q", "--", rel],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0


def reusable_node_modules(step: DepStep, source: str, wt: str) -> str:
    """Why the source's ``node_modules`` can't be cloned for ``step``, or
    ``""`` when it can."""
    if step.manager not in _NM_MARKER:
        return "%s installs from its own store" % step.manager
    if os.path.realpath(source) == os.path.realpath(wt):
        return "no separate source checkout"
    src_nm = os.path.join(source, step.dir, "node_modules")
    if os.path.islink(src_nm) or not os.path.isdir(src_nm):
        return "the source checkout has no installed node_modules"
    marker = os.path.join(src_nm, _NM_MARKER[step.manager])
    src_lock = os.path.join(source, step.dir, step.lockfile)
    try:
        if os.path.getmtime(marker) < os.path.getmtime(src_lock):
            return "the source's install is older than its lockfile"
    except OSError:
        return "the source's install did not finish"
    a = _sha256(src_lock)
    if not a or a != _sha256(os.path.join(wt, step.dir, step.lockfile)):
        return "the lockfile differs from the source checkout's"
    if step.manager == "yarn" and os.path.exists(
        os.path.join(wt, step.dir, ".yarnrc.yml")
    ):
        return "yarn 2+ project"
    # Trailing slash: the folder doesn't exist yet, and a "node_modules/"
    # pattern only matches what git knows is a directory.
    rel_nm = (step.dir + "/" if step.dir else "") + "node_modules/"
    if not _git_ignored(wt, rel_nm):
        return "node_modules is not git-ignored here"
    return ""


def install_command(step: DepStep, wt: str) -> List[str]:
    """The package manager's own install for ``step`` (argv)."""
    if step.manager == "uv":
        return ["uv", "sync", "--frozen"]
    if step.manager == "pnpm":
        return ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"]
    if step.manager == "yarn":
        if os.path.exists(os.path.join(wt, step.dir, ".yarnrc.yml")):
            return ["yarn", "install", "--immutable"]
        return ["yarn", "install", "--frozen-lockfile", "--prefer-offline"]
    return ["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"]


def step_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment an install runs in: the server's, minus what would
    point it at ANOTHER environment (the server's own venv)."""
    env = dict(os.environ if base is None else base)
    for k in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "CONDA_PREFIX"):
        env.pop(k, None)
    return env


def run_dependency_step(
    step: DepStep,
    source: str,
    wt: str,
    mode: str,
    log: IO[str],
    env: Optional[Dict[str, str]] = None,
    timeout: float = 900,
) -> dict:
    """Make ``step`` ready in worktree ``wt``; writes what it does to ``log``.

    Returns ``{"name", "how", "state", "seconds", "note"}``: ``how`` is
    ``reflink`` / ``clonefile`` / ``hardlink`` (cloned from ``source``),
    ``install`` (the package manager ran) or ``skipped``; ``state`` is
    ``ok`` / ``failed`` / ``skipped``. Never raises."""
    t0 = time.monotonic()
    out = {"name": step.name, "how": "skipped", "state": "skipped", "note": ""}

    def done(**kw) -> dict:
        out.update(kw)
        out["seconds"] = round(time.monotonic() - t0, 2)
        if out["note"]:
            log.write("  %s\n" % out["note"])
        log.flush()
        return out

    here = os.path.join(wt, step.dir)
    log.write("# %s\n" % step.name)
    log.flush()
    nm = os.path.join(here, "node_modules")
    if step.manager in _NM_MARKER and mode == "auto" and not os.path.lexists(nm):
        why = reusable_node_modules(step, source, wt)
        if not why:
            try:
                how = clone_tree(os.path.join(source, step.dir, "node_modules"), nm)
                if how == "hardlink":
                    _unshare_node_modules(nm)
                return done(
                    how=how,
                    state="ok",
                    note="node_modules cloned from the source checkout (%s; "
                    "lockfile identical)" % how,
                )
            except OSError as err:
                log.write("  clone failed (%s); installing instead\n" % err)
        else:
            log.write("  not reusing the source's node_modules: %s\n" % why)
    argv = install_command(step, wt)
    if not shutil.which(argv[0]):
        return done(note="%s is not installed on this machine — skipped" % argv[0])
    log.write("$ %s\n" % " ".join(argv))
    log.flush()
    try:
        cp = subprocess.run(
            argv,
            cwd=here,
            env=step_env(env),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return done(
            how="install",
            state="failed",
            note="%s timed out after %ds" % (" ".join(argv[:2]), timeout),
        )
    except OSError as err:
        return done(how="install", state="failed", note="%s: %s" % (argv[0], err))
    if cp.returncode != 0:
        return done(
            how="install",
            state="failed",
            note="%s in %s exited %d"
            % (" ".join(argv[:2]), step.dir or ".", cp.returncode),
        )
    return done(how="install", state="ok")
