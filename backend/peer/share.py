"""The one shared folder per link — creating it, reading it, committing it.

Layout (see :mod:`backend.peer.paths`)::

    shares/<share_id>/
      work/        the folder both agents edit (rw inside the sandbox)
      work/.git    gitfile -> repo.git (read-only inside the sandbox)
      repo.git/    the TRUSTED git dir (read-only inside the sandbox)
      home/        the sandboxed agent's $HOME
      run/         sockets, mcp.json, bridge.py; run/no-hooks (empty, ro)

Everything in ``work/`` is hostile: the sandboxed agent may have been
prompt-injected by the peer and may plant symlinks, FIFOs, nested repos,
``.gitattributes`` filters, a replacement ``.git`` directory — anything a
process can create in a directory it owns. The rules that keep host-side code
safe on it:

* host git on a share ALWAYS goes through :func:`git`, which pins ``GIT_DIR``
  and ``GIT_WORK_TREE`` (so ``work/.git`` is never consulted), ignores system
  and global config, turns off hooks and fsmonitor, and runs with a minimal
  env — so no config the agent can write is ever read, and no program the
  agent can write is ever run;
* file reads walk ``work/`` one component at a time with ``O_NOFOLLOW`` from
  a held directory fd (:func:`read_file`), so a symlink — even one swapped in
  mid-walk — can never lead outside;
* every :func:`read_file` failure is the same ``ShareError("not found")``, so
  the peer can't probe what exists outside the folder.

See ``docs/peer-link.md`` ("The shared folder — CONTRACT").
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from backend import gitutil
from backend.peer import paths

__all__ = [
    "Share",
    "ShareError",
    "GitResult",
    "create_share",
    "open_share",
    "git",
    "read_file",
    "list_files",
    "diff",
    "checkpoint",
    "export",
    "check_target",
    "fetch_into",
    "head_sha",
    "mirror_bundle",
    "MIRROR_REF",
    "MIRROR_MAX_BYTES",
    "remove_share",
    "validate_relpath",
    "sanitize_message",
    "AUTHOR_NAME",
    "AUTHOR_EMAIL",
    "READ_MAX_BYTES",
]

AUTHOR_NAME = "MindFlock peer"
AUTHOR_EMAIL = "peer@mindflock.invalid"
READ_MAX_BYTES = 524288
MAX_COMPONENTS = 64
MAX_RELPATH = 1024
MAX_NAME_BYTES = 255
MESSAGE_MAX = 500
GITLINK_MODE = "160000"
# A share's git output is capped while it is read, so a multi-GB file the
# agent wrote can't balloon the host process.
_GIT_OUT_CAP = 32 * 1024 * 1024
_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")
_BRANCH_MAX = 200
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")

# Keys written to repo.git/config by create_share. core.hooksPath is added
# per share (it names that share's run/no-hooks).
_HARDENED = (
    ("core.fsmonitor", "false"),
    ("core.untrackedCache", "false"),
    ("diff.ignoreSubmodules", "all"),
    ("status.submoduleSummary", "false"),
    ("submodule.recurse", "false"),
    ("protocol.allow", "never"),
    ("receive.denyCurrentBranch", "refuse"),
    ("gc.auto", "0"),
    ("commit.gpgSign", "false"),
)

# Passed with -c on EVERY host git call on a share: they win over any file.
# The attributes/excludes files would otherwise default to $HOME/.config/git/
# — and HOME is the agent's writable home.
_GIT_C = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.attributesFile=/dev/null",
    "core.excludesFile=/dev/null",
    "diff.ignoreSubmodules=all",
    "submodule.recurse=false",
    "protocol.allow=never",
    "commit.gpgSign=false",
    "gc.auto=0",
    "maintenance.auto=false",
)

# One host-side writer of a share's index at a time (checkpoint vs export).
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class ShareError(Exception):
    """A share operation failed. The message is safe to show the peer."""


@dataclass(frozen=True)
class Share:
    share_id: str
    root: str
    work: str
    gitdir: str
    home: str
    run: str


@dataclass
class GitResult:
    returncode: int
    stdout: bytes
    stderr: str
    truncated: bool = False


def _lock(share: Share) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(share.share_id, threading.Lock())


def _base_env(home: str) -> dict:
    env = {
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
    }
    return env


def _run(
    argv: List[str],
    *,
    env: dict,
    cwd: str,
    timeout: float,
    input: Optional[bytes] = None,
    max_output: Optional[int] = None,
) -> GitResult:
    """Run ``argv``; stdout is read up to ``max_output`` bytes (the process is
    killed past that), stderr goes to a temp file so neither pipe can block."""
    cap = _GIT_OUT_CAP if max_output is None else max_output
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err,
                cwd=cwd,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as e:
            raise ShareError("git unavailable: %s" % e.strerror) from None
        out = bytearray()
        truncated = False
        timed_out = threading.Event()

        def _expire() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, _expire)
        timer.daemon = True
        timer.start()
        try:
            if input is not None:
                threading.Thread(
                    target=_feed, args=(proc.stdin, input), daemon=True
                ).start()
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                room = cap - len(out)
                if len(chunk) > room:
                    out += chunk[:room]
                    truncated = True
                    proc.kill()
                    break
                out += chunk
            proc.stdout.close()
            rc = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        err.seek(0)
        stderr = err.read(4096).decode("utf-8", "replace")
    if timed_out.is_set():
        raise ShareError("git timed out")
    return GitResult(rc, bytes(out), stderr, truncated)


def _feed(pipe, data: bytes) -> None:
    try:
        pipe.write(data)
    except OSError:
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def git(
    share: Share,
    *args: str,
    check: bool = True,
    timeout: float = 60,
    input: Optional[bytes] = None,
    max_output: Optional[int] = None,
    extra_env: Optional[dict] = None,
) -> GitResult:
    """Run host git on ``share`` with the hardened environment.

    ``GIT_DIR``/``GIT_WORK_TREE`` are pinned to the trusted git dir and the
    work tree, so whatever the agent left at ``work/.git`` is never read.
    System and global config are off, hooks and fsmonitor are forced off with
    ``-c``, and the env is otherwise minimal. Raises :class:`ShareError` on a
    non-zero exit when ``check``."""
    env = _base_env(share.home)
    env["GIT_DIR"] = share.gitdir
    env["GIT_WORK_TREE"] = share.work
    if extra_env:
        env.update(extra_env)
    argv = ["git"]
    for kv in _GIT_C:
        argv += ["-c", kv]
    argv += list(args)
    res = _run(
        argv,
        env=env,
        cwd=share.work,
        timeout=timeout,
        input=input,
        max_output=max_output,
    )
    # A capped read kills git on purpose: that's truncation, not failure.
    if check and res.returncode != 0 and not res.truncated:
        sub = args[0] if args else "git"
        raise ShareError("git %s failed" % sub)
    return res


# --------------------------------------------------------------------------- #
# Create / open / remove
# --------------------------------------------------------------------------- #
def _share_from_id(share_id: str) -> Share:
    p = paths.share_paths(share_id)
    return Share(share_id, p["root"], p["work"], p["gitdir"], p["home"], p["run"])


def open_share(share_id: str) -> Share:
    """The :class:`Share` for an existing ``share_id`` (raises ShareError when
    it doesn't exist or its root is not a real directory under shares/)."""
    try:
        share = _share_from_id(share_id)
    except ValueError:
        raise ShareError("no such share") from None
    try:
        st = os.lstat(share.root)
    except OSError:
        raise ShareError("no such share") from None
    if not stat.S_ISDIR(st.st_mode):
        raise ShareError("no such share")
    return share


def _check_branch(name: str, what: str = "branch") -> None:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > _BRANCH_MAX
        or name.startswith("-")
        or _CTRL_RE.search(name)
    ):
        raise ShareError("bad %s name" % what)
    res = _run(
        ["git", "check-ref-format", "--branch", name],
        env=_base_env(tempfile.gettempdir()),
        cwd="/",
        timeout=10,
    )
    if res.returncode != 0 or res.stdout.decode("utf-8", "replace").strip() != name:
        raise ShareError("bad %s name" % what)


BASE_REF = "refs/mindflock/base"


def create_share(link_id: str, repo_path: str, branch: Optional[str] = None) -> Share:
    """Make a new shared folder: a shallow clone of the user's (trusted)
    ``repo_path`` with a separate, hardened git dir. See the module doc."""
    if not isinstance(link_id, str) or not _ID_RE.match(link_id):
        raise ShareError("bad link id")
    if not isinstance(repo_path, str) or not repo_path or "\x00" in repo_path:
        raise ShareError("bad repo path")
    repo = os.path.realpath(os.path.expanduser(repo_path))
    if not os.path.isdir(repo):
        raise ShareError("repo path is not a directory")
    if paths.is_inside_peer_root(repo):
        raise ShareError("repo path is inside the peer folder")
    if branch is not None:
        _check_branch(branch)

    # 80 bits: a local directory name (the peer never learns it), kept short
    # because it sits in the agent's socket paths and macOS caps those at 104
    # bytes (backend.peer.paths.unix_addr).
    share_id = secrets.token_hex(10)
    share = _share_from_id(share_id)
    paths.ensure_dir(paths.peer_root())
    paths.ensure_dir(paths.shares_dir())
    # A folder that isn't the top of a git repository is shared as a
    # snapshot: its files committed into a throwaway git dir (the folder
    # itself is never written), which the share then clones like a repo.
    snapshot = None
    if not _is_repo_top(repo):
        if branch is not None:
            raise ShareError(
                "that folder isn't a git repository, so there is no branch to pick"
            )
        snapshot = _snapshot(repo)
    os.mkdir(share.root, 0o700)
    try:
        _populate(share, snapshot or repo, branch)
        # A source repo with submodules leaves gitlinks in the clone's
        # index; drop them so the share starts with none (see _drop_gitlinks).
        if _drop_gitlinks(share):
            git(
                share,
                "commit",
                "--no-verify",
                "--no-gpg-sign",
                "--quiet",
                "-m",
                "MindFlock: shared folder without submodules",
                extra_env=_author_env(),
                timeout=120,
            )
        # The commit the share started from: diff() compares against it, so
        # the peer sees committed checkpoints too, not just loose edits. It
        # lives in repo.git, which the sandbox can't write.
        git(share, "update-ref", BASE_REF, "HEAD")
    except BaseException:
        _rmtree(share.root)
        raise
    finally:
        if snapshot:
            _rmtree(os.path.dirname(snapshot))
    return share


#: Never part of a snapshot: dependency trees, virtualenvs, build output and
#: caches — large, machine-specific, and regenerated by the project's own
#: tooling. (The folder's own .gitignore applies on top of these.)
SNAPSHOT_EXCLUDES = (
    "node_modules/",
    ".venv/",
    "venv/",
    "env/",
    "__pycache__/",
    "*.pyc",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".tox/",
    ".next/",
    ".nuxt/",
    ".turbo/",
    ".cache/",
    "dist/",
    "build/",
    "target/",
    ".gradle/",
    ".idea/",
    ".DS_Store",
    "Thumbs.db",
)
#: A snapshot bigger than this is refused (it would be a slow, huge share,
#: and almost certainly includes something that shouldn't travel).
SNAPSHOT_MAX_FILES = 20_000
SNAPSHOT_MAX_BYTES = 500 * 1024 * 1024


def _is_repo_top(path: str) -> bool:
    """Whether ``path`` is the top of a git work tree (what ``clone`` takes).
    A folder INSIDE a repository is not: cloning it would clone the whole
    repository, not the folder that was picked."""
    if not os.path.exists(os.path.join(path, ".git")):
        return False
    env = _base_env(tempfile.gettempdir())
    res = _run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"],
        env=env,
        cwd=path,
        timeout=30,
    )
    top = res.stdout.decode("utf-8", "replace").strip()
    return res.returncode == 0 and bool(top) and os.path.realpath(top) == path


def _snapshot(folder: str) -> str:
    """Commit ``folder``'s files into a fresh git dir under the peer root and
    return its path (the caller clones from it, then removes its parent).
    The folder is only READ: the git dir lives elsewhere (``--git-dir`` +
    ``--work-tree``). Honours the folder's .gitignore plus
    :data:`SNAPSHOT_EXCLUDES`; nested repositories come in as gitlinks, which
    the share then drops like any submodule. Raises :class:`ShareError`."""
    tmp = tempfile.mkdtemp(prefix="snap-", dir=paths.shares_dir())
    os.chmod(tmp, 0o700)
    gitdir = os.path.join(tmp, "snap.git")
    home = os.path.join(tmp, "home")
    os.mkdir(home, 0o700)
    env = _base_env(home)
    env.update(_author_env())
    base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.autocrlf=false",
        "--git-dir=" + gitdir,
        "--work-tree=" + folder,
    ]
    try:
        res = _run(base + ["init", "--quiet"], env=env, cwd=tmp, timeout=30)
        if res.returncode != 0:
            raise ShareError(
                "could not snapshot the folder: %s" % _first_line(res.stderr)
            )
        info = os.path.join(gitdir, "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "exclude"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(SNAPSHOT_EXCLUDES) + "\n")
        res = _run(base + ["add", "-A", "--", "."], env=env, cwd=tmp, timeout=600)
        if res.returncode != 0:
            raise ShareError(
                "could not snapshot the folder: %s" % _first_line(res.stderr)
            )
        listed = _run(
            base + ["ls-files", "-z", "--cached"], env=env, cwd=tmp, timeout=120
        )
        names = [n for n in listed.stdout.split(b"\0") if n]
        if not names:
            raise ShareError("that folder is empty — nothing to share")
        if len(names) > SNAPSHOT_MAX_FILES:
            raise ShareError(
                "that folder has %d files — too many to share as a snapshot "
                "(the limit is %d; add a .gitignore, or share a smaller folder)"
                % (len(names), SNAPSHOT_MAX_FILES)
            )
        size = 0
        for n in names:
            try:
                st = os.lstat(os.path.join(folder, os.fsdecode(n)))
            except OSError:
                continue
            size += st.st_size if stat.S_ISREG(st.st_mode) else 0
        if size > SNAPSHOT_MAX_BYTES:
            raise ShareError(
                "that folder holds %d MB — too much to share as a snapshot "
                "(the limit is %d MB; add a .gitignore, or share a smaller folder)"
                % (size // (1024 * 1024), SNAPSHOT_MAX_BYTES // (1024 * 1024))
            )
        res = _run(
            base
            + [
                "commit",
                "--quiet",
                "--no-verify",
                "--no-gpg-sign",
                "-m",
                "MindFlock: snapshot of %s" % (os.path.basename(folder) or "folder"),
            ],
            env=env,
            cwd=tmp,
            timeout=600,
        )
        if res.returncode != 0:
            raise ShareError(
                "could not snapshot the folder: %s" % _first_line(res.stderr)
            )
    except BaseException:
        _rmtree(tmp)
        raise
    return gitdir


def _base(share: Share) -> str:
    """The share's starting commit (``BASE_REF``), or HEAD for a share made
    before the ref existed."""
    res = git(share, "rev-parse", "--verify", "-q", BASE_REF + "^{commit}", check=False)
    sha = res.stdout.decode("ascii", "replace").strip()
    return (
        sha if res.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", sha) else "HEAD"
    )


_INFO_ATTRIBUTES = (
    "# Written by MindFlock (peer share). Overrides the folder's own\n"
    "# .gitattributes: no filter, diff or merge drivers, ever.\n"
    "* !filter !diff !merge !working-tree-encoding\n"
)


def _populate(share: Share, repo: str, branch: Optional[str]) -> None:
    os.chmod(share.root, 0o700)
    for d in (share.home, share.run):
        paths.ensure_dir(d)
    nohooks = os.path.join(share.run, "no-hooks")
    os.mkdir(nohooks, 0o700)

    env = _base_env(share.home)
    argv = ["git"]
    for kv in (
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "protocol.allow=never",
        "protocol.file.allow=always",  # the trusted local source only
    ):
        argv += ["-c", kv]
    argv += [
        "clone",
        "--quiet",
        "--no-local",
        "--depth",
        "1",
        "--single-branch",
        "--no-recurse-submodules",
        "--template=",
        "--separate-git-dir=" + share.gitdir,
    ]
    if branch is not None:
        argv += ["-b", branch]
    argv += ["--", "file://" + repo, share.work]
    res = _run(argv, env=env, cwd=share.root, timeout=600)
    if res.returncode != 0:
        raise ShareError("clone failed: %s" % _first_line(res.stderr))

    # Fresh config: carry over only what the object store needs to be read.
    old = os.path.join(share.gitdir, "config")
    keep = {}
    for key in ("core.repositoryformatversion", "extensions.objectformat"):
        r = _run(
            ["git", "config", "--file", old, "--get", key],
            env=env,
            cwd=share.root,
            timeout=10,
        )
        val = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode == 0 and re.match(r"^[A-Za-z0-9]{1,16}$", val):
            keep[key] = val
    lines = ["[core]"]
    lines.append(
        "\trepositoryformatversion = %s" % keep.get("core.repositoryformatversion", "0")
    )
    lines += ["\tbare = false", "\tlogallrefupdates = true", "\tfilemode = true"]
    lines.append('\thooksPath = "%s"' % _cfg_quote(nohooks))
    cfg_text = "\n".join(lines) + "\n"
    if "extensions.objectformat" in keep:
        cfg_text += (
            "[extensions]\n\tobjectformat = %s\n" % keep["extensions.objectformat"]
        )
    tmp = old + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(cfg_text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, old)
    for key, val in _HARDENED:
        r = _run(
            ["git", "config", "--file", old, key, val],
            env=env,
            cwd=share.root,
            timeout=10,
        )
        if r.returncode != 0:
            raise ShareError("could not harden git config")

    # No hooks, no info/ except exclude, no remotes (config was replaced, the
    # remote-tracking refs go too).
    hooks = os.path.join(share.gitdir, "hooks")
    _rmtree(hooks)
    os.mkdir(hooks, 0o700)
    info = os.path.join(share.gitdir, "info")
    if os.path.isdir(info) and not os.path.islink(info):
        for name in os.listdir(info):
            if name not in ("exclude", "attributes"):
                _rmtree(os.path.join(info, name))
    # info/attributes outranks every .gitattributes in the work tree, so the
    # folder can't name a filter/diff/merge driver for HOST git to run — not
    # even one the user's own global config defines (git-lfs, textconv
    # tools): the engine's ordinary git calls on this folder do read that.
    os.makedirs(info, mode=0o700, exist_ok=True)
    with open(os.path.join(info, "attributes"), "w", encoding="utf-8") as f:
        f.write(_INFO_ATTRIBUTES)
    _rmtree(os.path.join(share.gitdir, "refs", "remotes"))
    packed = os.path.join(share.gitdir, "packed-refs")
    if os.path.isfile(packed):
        with open(packed, "r", encoding="utf-8", errors="replace") as f:
            kept = [ln for ln in f if " refs/remotes/" not in ln]
        with open(packed, "w", encoding="utf-8") as f:
            f.writelines(kept)
    _rmtree(os.path.join(share.gitdir, "FETCH_HEAD"))

    gitfile = os.path.join(share.work, ".git")
    tmp = gitfile + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("gitdir: %s\n" % share.gitdir)
    os.replace(tmp, gitfile)
    os.chmod(gitfile, 0o400)
    for d in (share.work, share.gitdir):
        os.chmod(d, 0o700)
    os.chmod(nohooks, 0o500)


def _cfg_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _first_line(text: str) -> str:
    line = (text or "").strip().splitlines()[:1]
    return _CTRL_RE.sub("", line[0])[:200] if line else "unknown error"


def _rmtree(path: str) -> None:
    """Remove ``path`` without following symlinks (shutil's fd-based rmtree),
    restoring owner permissions the agent may have stripped from a directory
    in work/ — a 0500 or 0000 dir would otherwise stick."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return

    def onexc(func, p, exc):
        if not isinstance(exc, PermissionError):
            raise exc
        for d in (os.path.dirname(p), p):
            _chmod_dir_nofollow(d)
        if func in (os.rmdir, os.unlink, os.remove):
            func(p)
        else:
            shutil.rmtree(p, onexc=onexc)

    shutil.rmtree(path, onexc=onexc)


def _chmod_dir_nofollow(path: str) -> None:
    """chmod 0700 the directory at ``path`` only if it is not a symlink: an
    O_PATH|O_NOFOLLOW fd pins the inode, the chmod goes through /proc. Without
    O_PATH (macOS) the O_NOFOLLOW fd is opened for reading — which needs r on
    the dir — and fchmod'ed."""
    if not _O_PATH:
        try:
            fd = os.open(
                path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
        except OSError:
            return
        try:
            os.fchmod(fd, 0o700)
        except OSError:
            pass
        finally:
            os.close(fd)
        return
    try:
        fd = os.open(path, _O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.chmod("/proc/self/fd/%d" % fd, 0o700)
    except OSError:
        pass
    finally:
        os.close(fd)


def remove_share(
    share_id: str, is_running: Optional[Callable[[str], bool]] = None
) -> bool:
    """Delete a share's whole root. Refuses an id that doesn't name a real
    directory inside ``paths.shares_dir()``, and a share a session is still
    running on (``is_running(share_id)``). Returns False when it was already
    gone."""
    if not isinstance(share_id, str) or not paths.SHARE_ID_RE.match(share_id):
        raise ShareError("bad share id")
    root = paths.share_root(share_id)
    base = os.path.realpath(paths.shares_dir())
    if os.path.dirname(root) != paths.shares_dir():
        raise ShareError("share outside the shares folder")
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        raise ShareError("share root is not a directory")
    real = os.path.realpath(root)
    if os.path.dirname(real) != base or not real.startswith(base + os.sep):
        raise ShareError("share outside the shares folder")
    if is_running is not None and is_running(share_id):
        raise ShareError("a session is still running on this share")
    with _LOCKS_GUARD:
        _LOCKS.pop(share_id, None)
    _rmtree(real)
    return True


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def _is_git_name(name: str) -> bool:
    return name.casefold() == ".git"


def validate_relpath(relpath) -> List[str]:
    """``relpath`` → its components, or ShareError("not found").

    Rejects non-strings, absolute paths, NUL, ``..``, empty components, any
    ``.git`` component (case-insensitively), over-long paths/names and more
    than 64 components. ``.`` components are dropped. Everything else — a
    backslash, a look-alike Unicode dot — is a literal filename character."""
    nf = ShareError("not found")
    if not isinstance(relpath, str) or not relpath or len(relpath) > MAX_RELPATH:
        raise nf
    if "\x00" in relpath or relpath.startswith("/"):
        raise nf
    try:
        relpath.encode("utf-8")
    except UnicodeEncodeError:
        raise nf from None
    parts = []
    for comp in relpath.split("/"):
        if comp == ".":
            continue
        if comp in ("", "..") or _is_git_name(comp):
            raise nf
        if len(comp.encode("utf-8")) > MAX_NAME_BYTES:
            raise nf
        parts.append(comp)
    if not parts or len(parts) > MAX_COMPONENTS:
        raise nf
    return parts


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_O_PATH = getattr(os, "O_PATH", 0)


def _open_regular(share: Share, parts: List[str]) -> int:
    """fd of the regular file ``work/<parts…>``, opened without following a
    symlink at any component. Raises OSError/ShareError on anything else."""
    fd = os.open(share.work, _DIR_FLAGS)
    try:
        for comp in parts[:-1]:
            nxt = os.open(comp, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = parts[-1]
        if _O_PATH:
            # O_PATH opens nothing: a device or FIFO is never actually opened.
            pfd = os.open(leaf, _O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                st = os.fstat(pfd)
                _check_leaf(st)
                rfd = os.open(
                    "/proc/self/fd/%d" % pfd,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC,
                )
            finally:
                os.close(pfd)
        else:  # pragma: no cover — non-Linux
            rfd = os.open(
                leaf,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | os.O_NOCTTY
                | os.O_CLOEXEC,
                dir_fd=fd,
            )
            st = None
        try:
            st2 = os.fstat(rfd)
            _check_leaf(st2)
            if st is not None and (st.st_dev, st.st_ino) != (st2.st_dev, st2.st_ino):
                raise ShareError("not found")
        except BaseException:
            os.close(rfd)
            raise
        return rfd
    finally:
        os.close(fd)


def _check_leaf(st: os.stat_result) -> None:
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ShareError("not found")


def _read_fd(fd: int, max_bytes: int) -> bytes:
    out = bytearray()
    while len(out) <= max_bytes:
        chunk = os.read(fd, min(65536, max_bytes + 1 - len(out)))
        if not chunk:
            break
        out += chunk
    return bytes(out)


def _decode(data: bytes, truncated: bool):
    """(text, "utf-8") when ``data`` is UTF-8 (allowing a code point cut in
    half by truncation), else (base64, "base64")."""
    tries = (data, data[:-1], data[:-2], data[:-3]) if truncated else (data,)
    for cand in tries:
        try:
            text = cand.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\x00" not in text:
            return text, "utf-8"
        break
    return base64.b64encode(data).decode("ascii"), "base64"


def read_file(share: Share, relpath, max_bytes: int = READ_MAX_BYTES) -> dict:
    """``work/<relpath>`` → ``{path, size, encoding, content, truncated}``.

    Regular, singly-linked files only, reached without following any symlink;
    at most ``max_bytes`` are returned. Every failure is
    ``ShareError("not found")``."""
    parts = validate_relpath(relpath)
    try:
        max_bytes = max(0, int(max_bytes))
        fd = _open_regular(share, parts)
        try:
            size = os.fstat(fd).st_size
            data = _read_fd(fd, max_bytes)
        finally:
            os.close(fd)
    except (OSError, ShareError, ValueError, TypeError):
        raise ShareError("not found") from None
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    content, encoding = _decode(data, truncated)
    return {
        "path": "/".join(parts),
        "size": size,
        "encoding": encoding,
        "content": content,
        "truncated": truncated,
    }


def _split_z(raw: bytes, truncated: bool = False) -> List[str]:
    items = raw.split(b"\x00")
    if truncated and items:
        items.pop()  # cut mid-name
    out = []
    for item in items:
        if not item:
            continue
        try:
            out.append(item.decode("utf-8"))
        except UnicodeDecodeError:
            continue  # unreadable through read_file anyway
    return out


def _visible(path: str) -> bool:
    if not path or path.endswith("/"):
        return False  # a nested repo shows up as "dir/"
    return not any(_is_git_name(c) for c in path.split("/"))


def list_files(share: Share, limit: int = 5000) -> dict:
    """Tracked + untracked-not-ignored files → ``{files, truncated}``."""
    res = git(
        share,
        "ls-files",
        "-co",
        "--exclude-standard",
        "-z",
        "--",
        timeout=60,
        max_output=8 * 1024 * 1024,
    )
    seen = set()
    files = []
    truncated = res.truncated
    for p in _split_z(res.stdout, res.truncated):
        if p in seen or not _visible(p):
            continue
        seen.add(p)
        if len(files) >= limit:
            truncated = True
            break
        files.append(p)
    return {"files": files, "truncated": truncated}


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
_DIFF_FLAGS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--ignore-submodules=all",
    "--no-renames",
)


def _untracked(share: Share, limit: int = 5000) -> List[str]:
    res = git(
        share,
        "ls-files",
        "-o",
        "--exclude-standard",
        "-z",
        "--",
        max_output=8 * 1024 * 1024,
    )
    return [p for p in _split_z(res.stdout, res.truncated) if _visible(p)][:limit]


def _blocks(patch: str) -> List[str]:
    """Split a unified diff into units that are emitted whole: for each file,
    its header + first hunk, then each further hunk on its own (prefixed with
    nothing — the header has already gone out)."""
    out: List[str] = []
    files: List[List[str]] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            files.append([line])
        elif files:
            files[-1].append(line)
    for lines in files:
        header: List[str] = []
        hunks: List[List[str]] = []
        for line in lines:
            if line.startswith("@@"):
                hunks.append([line])
            elif hunks:
                hunks[-1].append(line)
            else:
                header.append(line)
        if not hunks:
            out.append("".join(header))
            continue
        out.append("".join(header) + "".join(hunks[0]))
        for h in hunks[1:]:
            out.append("".join(h))
    return out


def _untracked_patch(share: Share, path: str, budget: int):
    """(patch_text, added_lines) for an untracked file shown as new."""
    head = "diff --git a/%s b/%s\nnew file mode 100644\n" % (path, path)
    try:
        f = read_file(share, path, max_bytes=max(0, budget))
    except ShareError:
        return head + "(not a regular file, or unreadable)\n", None
    if f["encoding"] != "utf-8":
        return head + "Binary files /dev/null and b/%s differ\n" % path, None
    text = f["content"]
    lines = text.splitlines()
    body = "".join("+" + ln + "\n" for ln in lines)
    if text and not text.endswith(("\n", "\r")):
        body += "\\ No newline at end of file\n"
    if f["truncated"]:
        body += "(file truncated)\n"
    return (
        head
        + "--- /dev/null\n+++ b/%s\n@@ -0,0 +1,%d @@\n" % (path, len(lines))
        + body,
        len(lines),
    )


def diff(share: Share, max_chars: int = 50000) -> dict:
    """The share's changes since it was created → ``{stat, diff, truncated}``.

    The patch is ``git diff <base>`` (tracked changes, committed by a
    checkpoint or not; base = ``BASE_REF``) followed by untracked
    files shown as new; only whole hunks are included, up to ``max_chars``.
    The index is never touched (it belongs to the host)."""
    max_chars = max(0, int(max_chars))
    stat_rows: List[dict] = []
    base = _base(share)
    num = git(share, "diff", base, "--numstat", "-z", *_DIFF_FLAGS, "--")
    fields = num.stdout.split(b"\x00")
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if not rec:
            continue
        cols = rec.split(b"\t", 2)
        if len(cols) != 3:
            continue
        try:
            path = cols[2].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not _visible(path):
            continue
        a, d = cols[0].decode("ascii", "replace"), cols[1].decode("ascii", "replace")
        stat_rows.append(
            {
                "path": path,
                "added": int(a) if a.isdigit() else None,
                "deleted": int(d) if d.isdigit() else None,
                "status": "modified",
            }
        )
    patch_res = git(
        share,
        "diff",
        base,
        "--patch",
        *_DIFF_FLAGS,
        "--",
        max_output=max(max_chars * 4, 65536) + 65536,
    )
    patch = patch_res.stdout.decode("utf-8", "replace")
    truncated = patch_res.truncated
    blocks = _blocks(patch)
    if patch_res.truncated and blocks:
        blocks.pop()  # the last unit may be cut mid-way

    out: List[str] = []
    used = 0
    for b in blocks:
        if used + len(b) > max_chars:
            truncated = True
            break
        out.append(b)
        used += len(b)
    for path in _untracked(share):
        if truncated:
            # The patch is full; untracked files still belong in the stat.
            stat_rows.append(
                {"path": path, "added": None, "deleted": 0, "status": "untracked"}
            )
            continue
        text, added = _untracked_patch(share, path, max_chars - used + 1)
        stat_rows.append(
            {"path": path, "added": added, "deleted": 0, "status": "untracked"}
        )
        if used + len(text) > max_chars:
            truncated = True
            continue
        out.append(text)
        used += len(text)
    return {"stat": stat_rows[:5000], "diff": "".join(out), "truncated": truncated}


# --------------------------------------------------------------------------- #
# Commit / export
# --------------------------------------------------------------------------- #
def sanitize_message(message) -> str:
    """A commit message the agent supplied: controls stripped (newlines and
    tabs kept), at most 500 chars, never empty."""
    text = message if isinstance(message, str) else ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL_RE.sub("", text).strip()
    text = text[:MESSAGE_MAX].strip()
    return text or "peer checkpoint"


def _author_env(author: Optional[Tuple[str, str]] = None) -> dict:
    """Who a share commit is by: ``author`` (name, email) when given, else
    the anonymous :data:`AUTHOR_NAME` — a checkpoint the peer's agent asked
    for is nobody's in particular."""
    name, email = author or (AUTHOR_NAME, AUTHOR_EMAIL)
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def _export_author() -> Optional[Tuple[str, str]]:
    """The person's git identity (docs/people.md) for the export commit — an
    export is the person's own act into their own repo — or None when it
    isn't set (the anonymous peer author, as before). Never raises."""
    try:
        from backend.web.core import person as _person

        return _person.git_author()
    except Exception:  # noqa: BLE001
        return None


def _head(share: Share) -> str:
    res = git(share, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    sha = res.stdout.decode("ascii", "replace").strip()
    if not re.match(r"^[0-9a-f]{40,64}$", sha):
        raise ShareError("share has no HEAD")
    return sha


def checkpoint(share: Share, message) -> str:
    """Commit everything in ``work/`` (minus nested repos) → the new HEAD sha.

    ``git add -A``, then every gitlink (mode 160000) is dropped from the
    index, so a nested repo the agent made never becomes a submodule; the
    commit skips hooks. With nothing to commit, returns the current HEAD."""
    with _lock(share):
        return _checkpoint_locked(share, sanitize_message(message))


def _drop_gitlinks(share: Share) -> bool:
    """Remove every gitlink (submodule) entry from the share's index.

    Host git only treats a path as a submodule when the TRUSTED index says
    so; with none there, a nested repo the agent builds in ``work/`` stays
    an untracked directory that host git never enters. True if any went."""
    staged = git(share, "ls-files", "--stage", "-z", "--")
    gitlinks = []
    for rec in staged.stdout.split(b"\x00"):
        if rec.startswith(GITLINK_MODE.encode() + b" "):
            path = rec.split(b"\t", 1)[1] if b"\t" in rec else b""
            if path:
                gitlinks.append(path)
    if gitlinks:
        git(
            share,
            "update-index",
            "--force-remove",
            "-z",
            "--stdin",
            input=b"\x00".join(gitlinks) + b"\x00",
        )
    return bool(gitlinks)


def _checkpoint_locked(
    share: Share, msg: str, author: Optional[Tuple[str, str]] = None
) -> str:
    git(share, "add", "-A", "--", timeout=120)
    _drop_gitlinks(share)
    changed = git(
        share,
        "diff",
        "--cached",
        "--quiet",
        "--ignore-submodules=none",
        "HEAD",
        "--",
        check=False,
    )
    if changed.returncode == 0:
        return _head(share)
    if changed.returncode != 1:
        raise ShareError("git diff failed")
    git(
        share,
        "commit",
        "--no-verify",
        "--no-gpg-sign",
        "--quiet",
        "--cleanup=verbatim",
        "-F",
        "-",
        input=msg.encode("utf-8"),
        extra_env=_author_env(author),
        timeout=120,
    )
    return _head(share)


def _trusted_env() -> dict:
    """The user's own env for git in their own (trusted) repo, minus any
    variable that could redirect git at another repository."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_")
        or k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _target_git(target: str, *args: str, timeout: float = 120) -> GitResult:
    argv = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "fetch.recurseSubmodules=false",
        "-c",
        "protocol.file.allow=always",
        "-C",
        target,
    ] + list(args)
    return _run(argv, env=_trusted_env(), cwd=target, timeout=timeout)


def check_target(
    target_repo: str, branch_name: str, share: Optional[Share] = None
) -> str:
    """Validate a trusted target for :func:`fetch_into` → its canonical path.

    ``branch_name`` must start with ``peer/`` and be a valid branch;
    ``target_repo`` must be an existing git repository outside the peer
    folder (and outside ``share``), with ``branch_name`` checked out in none
    of its worktrees. Shared by export and the standing mirror."""
    if not isinstance(branch_name, str) or not branch_name.startswith("peer/"):
        raise ShareError("branch name must start with peer/")
    _check_branch(branch_name)
    if not isinstance(target_repo, str) or not target_repo or "\x00" in target_repo:
        raise ShareError("bad target repo")
    target = os.path.realpath(os.path.expanduser(target_repo))
    if paths.is_inside_peer_root(target):
        raise ShareError("target repo is inside the peer folder")
    if share is not None:
        root = os.path.realpath(share.root)
        if target == root or target.startswith(root + os.sep):
            raise ShareError("target repo is inside the peer folder")
    if not os.path.isdir(target):
        raise ShareError("target repo is not a directory")
    probe = _target_git(target, "rev-parse", "--git-dir", timeout=30)
    if probe.returncode != 0:
        raise ShareError("target is not a git repository")
    ref = "refs/heads/" + branch_name
    wt = _target_git(target, "worktree", "list", "--porcelain", timeout=30)
    if wt.returncode != 0:
        raise ShareError("could not list the target's worktrees")
    worktrees = gitutil.parse_worktree_list(wt.stdout.decode("utf-8", "replace"))
    if any(w.get("branch") == ref for w in worktrees):
        raise ShareError("branch %s is checked out in the target" % branch_name)
    return target


def fetch_into(
    target: str,
    source: str,
    src_ref: str,
    branch_name: str,
    sha: str,
    *,
    what: str = "export",
    untrusted: bool = False,
) -> None:
    """The fetch step of export and the mirror: ``git fetch <source>
    +<src_ref>:refs/heads/<branch_name>`` in the TRUSTED ``target`` (already
    passed :func:`check_target`), then check the branch landed on ``sha``.
    Only a ref moves — the target's working tree and index are never touched.
    ``source`` is a git dir or a bundle file; ``untrusted`` (a bundle the
    peer built) also fscks every object received."""
    ref = "refs/heads/" + branch_name
    fsck = ("-c", "fetch.fsckObjects=true") if untrusted else ()
    fetch = _target_git(
        target,
        *fsck,
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--end-of-options",
        source,
        "+%s:%s" % (src_ref, ref),
        timeout=600,
    )
    if fetch.returncode != 0:
        raise ShareError("%s failed: %s" % (what, _first_line(fetch.stderr)))
    got = _target_git(target, "rev-parse", "--verify", "-q", ref + "^{commit}")
    if got.stdout.decode("ascii", "replace").strip() != sha:
        raise ShareError("%s failed: branch not updated" % what)


#: :func:`export` into a repo without the commits a shallow share starts
#: from (git would refuse the ref silently).
NO_SHARED_HISTORY = "target lacks the shared folder's history"


def _shallow_base_missing(share: Share, target: str) -> bool:
    """A git share is a shallow clone: fetching it into a repo that lacks
    its base commits leaves the branch unmoved (``fetch`` refuses refs that
    would need ``.git/shallow`` updated, and exits 0). True when ``target``
    lacks one — only the repo the folder was shared from, or a clone of it,
    has them. A snapshot share has no shallow base."""
    try:
        with open(os.path.join(share.gitdir, "shallow"), "rb") as fh:
            bases = [ln.strip().decode("ascii", "replace") for ln in fh]
    except OSError:
        return False
    for sha in bases:
        if not _SHA_RE.match(sha):
            continue
        probe = _target_git(target, "cat-file", "-e", sha + "^{commit}", timeout=30)
        if probe.returncode != 0:
            return True
    return False


def _commits_brought(target: str, sha: str, ref: str, old: str) -> int:
    """How many commits on ``ref`` (now at ``sha``) the fetch brought: not on
    the branch before (``old``) nor on any other ref of ``target``."""
    args = ["rev-list", "--count", sha]
    if old:
        args.append("^" + old)
    args += ["--not", "--exclude=" + ref, "--all"]
    got = _target_git(target, *args, timeout=60)
    try:
        return int(got.stdout.decode("ascii", "replace").strip() or 0)
    except ValueError:
        return 0


def export(share: Share, target_repo: str, branch_name: str) -> dict:
    """Checkpoint the share, then fetch its HEAD into the user's TRUSTED
    ``target_repo`` as ``refs/heads/<branch_name>`` (which must start with
    ``peer/``). Nothing from the share is checked out or executed there.
    → ``{branch, sha, target, commits}`` — ``commits``: how many the branch
    gained that ``target`` didn't already have (0: nothing new)."""
    target = check_target(target_repo, branch_name, share)
    if _shallow_base_missing(share, target):
        raise ShareError(NO_SHARED_HISTORY)
    ref = "refs/heads/" + branch_name
    before = _target_git(target, "rev-parse", "--verify", "-q", ref + "^{commit}")
    old = before.stdout.decode("ascii", "replace").strip()
    old = old if before.returncode == 0 and _SHA_RE.match(old) else ""
    with _lock(share):  # HEAD can't move between the commit and the fetch
        sha = _checkpoint_locked(
            share, "peer export to %s" % branch_name, author=_export_author()
        )
        fetch_into(target, share.gitdir, "HEAD", branch_name, sha)
    commits = _commits_brought(target, sha, ref, old)
    return {"branch": branch_name, "sha": sha, "target": target, "commits": commits}


# --------------------------------------------------------------------------- #
# The standing mirror, sharing side (docs/peer-link.md, "The mirror")
# --------------------------------------------------------------------------- #
#: The share-side ref a mirror bundle is cut from: the parent-rewritten copy
#: of HEAD's first-parent chain (see :func:`mirror_bundle`).
MIRROR_REF = "refs/mindflock/mirror"
#: A mirror bundle bigger than this is refused (it crosses the link in
#: ``mirror_pull`` chunks, under the link's request rate).
MIRROR_MAX_BYTES = 64 * 1024 * 1024
#: The newest commits of the chain a mirror carries; the oldest becomes a root.
MIRROR_MAX_COMMITS = 1000
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DROP_HEADERS = (b"parent ", b"gpgsig ", b"gpgsig-sha256 ", b"mergetag ")


def head_sha(share: Share) -> str:
    """The share's HEAD sha (what ``status`` answers as ``head``)."""
    return _head(share)


def _rewrite_commit(raw: bytes, parent: Optional[str]) -> bytes:
    """``raw`` (a commit object's body) with its parents replaced by
    ``parent`` (or none) and its signatures dropped; all else verbatim."""
    header, sep, message = raw.partition(b"\n\n")
    out: List[bytes] = []
    dropping = False
    for line in header.split(b"\n"):
        if dropping and line.startswith(b" "):
            continue  # a dropped header's continuation line
        dropping = line.startswith(_DROP_HEADERS)
        if dropping:
            continue
        out.append(line)
        if parent and line.startswith(b"tree "):
            out.append(b"parent " + parent.encode("ascii"))
    return b"\n".join(out) + sep + message


def _object_id(kind: bytes, body: bytes, sha256: bool) -> str:
    import hashlib

    h = hashlib.sha256() if sha256 else hashlib.sha1()  # noqa: S324 — git's id
    h.update(kind + b" %d\x00" % len(body) + body)
    return h.hexdigest()


def _mirror_chain(share: Share) -> Tuple[str, List[str]]:
    """Write the mirror copy of HEAD's chain → ``(head, [rewritten shas,
    oldest first])`` and point :data:`MIRROR_REF` at the last one.

    A share is a SHALLOW clone: its base commit names parents that exist only
    in the sharer's own repo, so a bundle of the real commits can't be
    fetched anywhere else ("did not send all necessary objects"). The copy
    is the same commits — same trees, authors, dates and messages — with the
    base made a root and every parent pointing at the previous copy. It is
    deterministic, so the receiver's last mirrored sha names a commit the
    sharer can cut the next (incremental) bundle from. Caller holds the lock."""
    head = _head(share)
    revs = git(
        share,
        "rev-list",
        "--first-parent",
        "--reverse",
        "--max-count=%d" % MIRROR_MAX_COMMITS,
        "HEAD",
    ).stdout.split()
    res = git(share, "cat-file", "--batch", input=b"\n".join(revs) + b"\n", timeout=120)
    if res.truncated:
        raise ShareError("share history is too big to mirror")
    batch = res.stdout
    sha256 = len(head) == 64
    chain: List[str] = []
    bodies: Dict[str, bytes] = {}
    pos = 0
    for _ in revs:
        nl = batch.index(b"\n", pos)
        fields = batch[pos:nl].split()
        if len(fields) != 3 or fields[1] != b"commit":
            raise ShareError("share history is unreadable")
        size = int(fields[2])
        raw = batch[nl + 1 : nl + 1 + size]
        pos = nl + 1 + size + 1
        body = _rewrite_commit(raw, chain[-1] if chain else None)
        oid = _object_id(b"commit", body, sha256)
        bodies[oid] = body
        chain.append(oid)
    if not chain:
        raise ShareError("share has no HEAD")
    have = git(
        share,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype)",
        input="\n".join(chain).encode("ascii") + b"\n",
        check=False,
    ).stdout.decode("ascii", "replace")
    # A missing object reads "<sha> missing": only "<sha> commit" is there.
    present = {
        f[0] for f in (ln.split() for ln in have.splitlines()) if f[1:] == ["commit"]
    }
    for oid in chain:
        if oid in present:
            continue
        wrote = git(
            share, "hash-object", "-t", "commit", "-w", "--stdin", input=bodies[oid]
        )
        if wrote.stdout.decode("ascii", "replace").strip() != oid:
            raise ShareError("share history could not be copied")
    git(share, "update-ref", MIRROR_REF, chain[-1])
    return head, chain


def mirror_bundle(share: Share, have: str, out_path: str) -> dict:
    """Cut the mirror bundle for a receiver whose mirror branch is at
    ``have`` ("" for none) into ``out_path`` → ``{head, tip, size}``.

    ``tip`` is the mirror copy of ``head`` (:func:`_mirror_chain`). When
    ``have`` is that copy already, nothing is written (``size`` 0); when it
    is an older copy, the bundle carries only what follows it; otherwise all
    of it. The receiver fetches ``MIRROR_REF`` from the bundle. Raises
    ShareError("too big to mirror") past :data:`MIRROR_MAX_BYTES`."""
    with _lock(share):
        head, chain = _mirror_chain(share)
        tip = chain[-1]
        if have == tip:
            return {"head": head, "tip": tip, "size": 0}
        args = ["bundle", "create", "--quiet", out_path, MIRROR_REF]
        if have and _SHA_RE.match(have) and have in chain:
            args.append("^" + have)
        git(share, *args, timeout=600)
    try:
        size = os.stat(out_path).st_size
    except OSError:
        raise ShareError("mirror bundle failed") from None
    if size > MIRROR_MAX_BYTES:
        with contextlib.suppress(OSError):
            os.unlink(out_path)
        raise ShareError("too big to mirror")
    return {"head": head, "tip": tip, "size": size}
