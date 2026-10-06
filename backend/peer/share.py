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
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

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

    share_id = secrets.token_hex(16)
    share = _share_from_id(share_id)
    paths.ensure_dir(paths.peer_root())
    paths.ensure_dir(paths.shares_dir())
    os.mkdir(share.root, 0o700)
    try:
        _populate(share, repo, branch)
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
    return share


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
    O_PATH|O_NOFOLLOW fd pins the inode, the chmod goes through /proc."""
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


def _author_env() -> dict:
    return {
        "GIT_AUTHOR_NAME": AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
    }


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


def _checkpoint_locked(share: Share, msg: str) -> str:
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
        extra_env=_author_env(),
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


def export(share: Share, target_repo: str, branch_name: str) -> dict:
    """Checkpoint the share, then fetch its HEAD into the user's TRUSTED
    ``target_repo`` as ``refs/heads/<branch_name>`` (which must start with
    ``peer/``). Nothing from the share is checked out or executed there.
    → ``{branch, sha, target}``."""
    if not isinstance(branch_name, str) or not branch_name.startswith("peer/"):
        raise ShareError("branch name must start with peer/")
    _check_branch(branch_name)
    if not isinstance(target_repo, str) or not target_repo or "\x00" in target_repo:
        raise ShareError("bad target repo")
    target = os.path.realpath(os.path.expanduser(target_repo))
    if paths.is_inside_peer_root(target):
        raise ShareError("target repo is inside the peer folder")
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
    for line in wt.stdout.decode("utf-8", "replace").splitlines():
        if line.strip() == "branch " + ref:
            raise ShareError("branch %s is checked out in the target" % branch_name)

    with _lock(share):  # HEAD can't move between the commit and the fetch
        sha = _checkpoint_locked(share, "peer export to %s" % branch_name)
        fetch = _target_git(
            target,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "--end-of-options",
            share.gitdir,
            "+HEAD:" + ref,
            timeout=600,
        )
    if fetch.returncode != 0:
        raise ShareError("export failed: %s" % _first_line(fetch.stderr))
    got = _target_git(target, "rev-parse", "--verify", "-q", ref + "^{commit}")
    if got.stdout.decode("ascii", "replace").strip() != sha:
        raise ShareError("export failed: branch not updated")
    return {"branch": branch_name, "sha": sha, "target": target}
