"""A session's branch as a standalone git bundle — for a device that has no
checkout of the repository (``send_session`` ``via: "bundle"``; docs/
devices.md "When the other device has no checkout").

**Export** (:func:`export`, the sending device). A bundle of the branch as it
is would need the repository's whole history on the other side (a thin
bundle names prerequisites the receiver doesn't have) or carry all of it (too
much for a large repository). So the export re-roots the branch: ONE snapshot
commit holding the tree at the fork point (where the branch left its base),
then each of the session's commits since — first-parent, at most
:data:`MAX_COMMITS`, same trees, messages, authors and dates, new hashes. The
bundle is the fork-point checkout plus the session's deltas, and verifies on
its own (no prerequisites). With ``paths`` only those paths' trees travel; a
single folder becomes the new repository's root (``tripwire/`` → a
``tripwire`` repository whose history is the commits that touched it).

Files that look like secrets or agent logins (:func:`backend.web.core.
send_files.classify`, plus the repository's ``.secrets.baseline`` names) are
left out of EVERY re-created commit's tree, each judged on its own — so a
secret committed and later deleted is not in the history either — listed,
never carried.
The work happens in a throwaway bare repository whose ``alternates`` point at
the session's object store: nothing is written to the session's repository.

**Unpack** (:func:`unpack`, the receiving device). The bundle is untrusted
input: a NEW repository (``git init --template=`` — no hook templates), the
system and global git config off (no filter drivers, fsmonitor or hooks from
them), ``core.hooksPath=/dev/null``, ``git bundle verify``, exactly one head
— ``refs/heads/<branch>`` — fetched with ``fsckObjects`` on, then checked out
with ``core.symlinks=false`` (a symlink in the tree becomes a small text
file) and ``core.fileMode=false``; every file then loses its executable and
set-id bits (recorded, so an agent can restore one on purpose). Before the
checkout the caller's ``check`` sees the tree (paths, sizes, object count):
the inbox refuses a repository-shaped folder, more files than its limit and
a checkout bigger than its quota has left. Nothing in the tree is ever run.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

#: The most commits re-created on the snapshot; older ones fold into it.
MAX_COMMITS = 200
GIT_TIMEOUT_S = 120.0

_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # pragma: allowlist secret (git's empty tree)


class BundleError(Exception):
    """A git step failed: ``code`` + a plain sentence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _env(extra: Optional[Dict[str, str]] = None, *, isolated: bool = False) -> dict:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    # Never inherit a caller's repository selection.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(key, None)
    env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
    if isolated:
        # The receiving side: none of the person's global or system config —
        # a filter driver, fsmonitor or hooksPath there must not run on
        # content that arrived from elsewhere.
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
    if extra:
        env.update(extra)
    return env


def _run(
    args: List[str],
    *,
    env: Optional[dict] = None,
    stdin: Optional[bytes] = None,
    timeout: float = GIT_TIMEOUT_S,
) -> Tuple[int, bytes, str]:
    try:
        cp = subprocess.run(
            ["git", *args],
            input=stdin,
            capture_output=True,
            timeout=timeout,
            env=env if env is not None else _env(),
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return -1, b"", str(err)
    return (
        cp.returncode,
        cp.stdout or b"",
        (cp.stderr or b"").decode("utf-8", "replace").strip(),
    )


def _ok(args: List[str], **kw) -> str:
    rc, out, err = _run(args, **kw)
    if rc != 0:
        raise BundleError(
            "bundle_failed",
            "git %s failed: %s" % (args[2] if len(args) > 2 else args[0], err or rc),
        )
    return out.decode("utf-8", "replace").strip()


def valid_branch(name: str) -> bool:
    """A branch name safe to recreate on the other side."""
    if not _BRANCH_RE.match(name or "") or name.startswith(("-", "/")):
        return False
    rc, _, _ = _run(["check-ref-format", "--branch", name])
    return rc == 0


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def _norm_paths(paths: List[str]) -> List[str]:
    out: List[str] = []
    for raw in paths or []:
        p = str(raw or "").replace("\\", "/").strip().strip("/")
        if not p or p == ".":
            continue
        parts = p.split("/")
        if any(seg in ("", ".", "..") for seg in parts) or "\x00" in p:
            raise BundleError(
                "bad_path", "%r is not a path inside the repository" % raw
            )
        if p not in out:
            out.append(p)
    return out


def _export(
    tmp: str,
    wt: str,
    branch: str,
    fork: str,
    out_dir: str,
    *,
    paths: Optional[List[str]] = None,
    classify: Callable[[str], Optional[str]] = lambda rel: None,
    max_commits: int = MAX_COMMITS,
) -> dict:
    """Write ``<out_dir>/<name>.bundle`` → ``{path, name, branch, commits,
    folded, scope, left_out, size}``. ``fork`` = the commit the branch left
    its base at ("" = none known: the last ``max_commits`` first-parent
    commits travel, the rest fold into the snapshot). ``paths``: repository-
    relative paths to keep (``[]`` = everything). Raises :class:`BundleError`."""
    if not valid_branch(branch):
        raise BundleError("bad_branch", "branch %r cannot be bundled" % branch)
    top = _ok(["-C", wt, "rev-parse", "--show-toplevel"])
    common = _ok(["-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    objects = os.path.join(common, "objects")
    keep = _norm_paths(paths or [])
    tip = _ok(["-C", wt, "rev-parse", "--verify", "HEAD^{commit}"])

    repo = os.path.join(tmp, "export.git")
    _ok(["init", "--bare", "--template=", "-q", repo])
    with open(os.path.join(repo, "objects", "info", "alternates"), "w") as f:
        f.write(objects + "\n")
    # --literal-pathspecs: a kept path is a path, never pathspec magic.
    g = ["--literal-pathspecs", "--git-dir", repo]

    # The first-parent line from the fork point (or the last max_commits).
    rng = ("%s..%s" % (fork, tip)) if fork else tip
    line = _ok(g + ["rev-list", "--first-parent", "--reverse", rng]).split()
    base = fork
    folded = 0
    if len(line) > max_commits:
        folded = len(line) - max_commits
        base = line[folded - 1]
        line = line[folded:]

    for p in keep:
        rc, _, _ = _run(g + ["cat-file", "-e", "%s:%s" % (tip, p)])
        if rc != 0:
            raise BundleError(
                "bad_path",
                "%s is not in the branch's last commit (commit it first)" % p,
            )
    sub = ""
    if len(keep) == 1:
        kind = _ok(g + ["cat-file", "-t", "%s:%s" % (tip, keep[0])])
        if kind == "tree":
            sub = keep[0]

    # Secrets and agent config are left out of EVERY re-created commit's
    # tree, judged per tree: a file committed and later deleted must not
    # survive in the history the bundle carries, whatever the fork point.
    scope = ["--"] + keep if keep else []
    index = os.path.join(tmp, "index")
    tree_cache: Dict[str, str] = {}
    verdicts: Dict[str, bool] = {}
    dropped: set = set()

    def left_out(path: str) -> bool:
        if path not in verdicts:
            verdicts[path] = bool(classify(path))
        return verdicts[path]

    def tree_for(commit: str) -> str:
        if commit in tree_cache:
            return tree_cache[commit]
        listing = _run(g + ["ls-tree", "-r", "-z", "--full-tree", commit] + scope)
        if listing[0] != 0:
            raise BundleError("bundle_failed", "git ls-tree failed: %s" % listing[2])
        rows = []
        cut = False
        for rec in listing[1].split(b"\x00"):
            if not rec:
                continue
            meta, _, name = rec.partition(b"\t")
            path = name.decode("utf-8", "surrogateescape")
            if left_out(path):
                dropped.add(path)
                cut = True
                continue
            if sub:
                if not path.startswith(sub + "/"):
                    continue
                path = path[len(sub) + 1 :]
            rows.append(meta + b"\t" + path.encode("utf-8", "surrogateescape"))
        if not keep and not cut:
            t = _ok(g + ["rev-parse", "%s^{tree}" % commit])
        elif sub and not cut:
            rc, out, _ = _run(
                g + ["rev-parse", "--verify", "-q", "%s:%s" % (commit, sub)]
            )
            t = out.decode().strip() if rc == 0 else _EMPTY_TREE
        else:
            env = _env({"GIT_INDEX_FILE": index})
            if os.path.exists(index):
                os.unlink(index)
            _ok(g + ["read-tree", "--empty"], env=env)
            if rows:
                rc, _, err = _run(
                    g + ["update-index", "-z", "--index-info"],
                    env=env,
                    stdin=b"\x00".join(rows) + b"\x00",
                )
                if rc != 0:
                    raise BundleError(
                        "bundle_failed", "git update-index failed: %s" % err
                    )
            t = _ok(g + ["write-tree"], env=env)
        tree_cache[commit] = t
        return t

    def commit_tree(tree: str, parent: str, src: str, message: bytes) -> str:
        env = _env()
        if src:
            fmt = _ok(
                g
                + [
                    "log",
                    "-1",
                    "--format=%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd",
                    "--date=raw",
                    src,
                ]
            )
            an, ae, ad, cn, ce, cd = (fmt.split("\x00") + [""] * 6)[:6]
            env.update(
                GIT_AUTHOR_NAME=an or "MindFlock",
                GIT_AUTHOR_EMAIL=ae or "mindflock@localhost",
                GIT_AUTHOR_DATE=ad,
                GIT_COMMITTER_NAME=cn or "MindFlock",
                GIT_COMMITTER_EMAIL=ce or "mindflock@localhost",
                GIT_COMMITTER_DATE=cd,
            )
        else:
            env.update(
                GIT_AUTHOR_NAME="MindFlock",
                GIT_AUTHOR_EMAIL="mindflock@localhost",
                GIT_COMMITTER_NAME="MindFlock",
                GIT_COMMITTER_EMAIL="mindflock@localhost",
            )
        args = g + ["commit-tree", "--no-gpg-sign", tree]
        if parent:
            args += ["-p", parent]
        rc, out, err = _run(args + ["-F", "-"], env=env, stdin=message)
        if rc != 0:
            raise BundleError("bundle_failed", "git commit-tree failed: %s" % err)
        return out.decode().strip()

    head = head_tree = ""
    if base:
        short = base[:12]
        msg = (
            "MindFlock snapshot: where %s started (%s)\n\nThe tree of %s as it "
            "was at %s. The commits after this one are the session's own, "
            "re-created on this snapshot by MindFlock's send (new hashes, same "
            "changes, authors and dates).\n"
            % (branch, short, sub or "the repository", short)
        ).encode()
        head_tree = tree_for(base)
        head = commit_tree(head_tree, "", "", msg)
    made = 0
    for c in line:
        t = tree_for(c)
        if head and (keep or dropped) and t == head_tree:
            continue  # this commit changed nothing in the paths that travel
        msg = _run(g + ["log", "-1", "--format=%B", c])[1] or b"(no message)\n"
        head = commit_tree(t, head, c, msg)
        head_tree = t
        made += 1
    if not head:
        raise BundleError("bundle_failed", "nothing to bundle on %s" % branch)
    _ok(g + ["update-ref", "refs/heads/" + branch, head])
    name = os.path.basename(sub) if sub else os.path.basename(top.rstrip("/"))
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name).strip(".-") or "work"
    out_path = os.path.join(out_dir, name + ".bundle")
    rc, _, err = _run(g + ["bundle", "create", "-q", out_path, "refs/heads/" + branch])
    if rc != 0:
        raise BundleError("bundle_failed", "git bundle create failed: %s" % err)
    return {
        "path": out_path,
        "name": name,
        "branch": branch,
        "commits": made,
        "folded": folded,
        "snapshot": bool(base),
        "scope": sub or (keep if keep else ""),
        "left_out": sorted(dropped),
        "size": os.path.getsize(out_path),
    }


def export(wt: str, branch: str, fork: str, out_dir: str, **kw) -> dict:
    """Write ``<out_dir>/<name>.bundle`` → ``{path, name, branch, commits,
    folded, snapshot, scope, left_out, size}``. ``fork`` = the commit the
    branch left its base at ("" = none known: the last ``max_commits``
    first-parent commits travel, older ones fold into the snapshot).
    ``paths``: repository-relative paths to keep (``[]`` = everything);
    ``classify(rel)`` → truthy for a path to leave out. Raises
    :class:`BundleError`. The scratch repository is always removed."""
    tmp = tempfile.mkdtemp(prefix="mf-export-", dir=out_dir)
    try:
        return _export(tmp, wt, branch, fork, out_dir, **kw)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# unpack (the receiving device)
# --------------------------------------------------------------------------- #
def _strip_modes(root: str) -> List[str]:
    """chmod every file under ``root`` (``.git`` aside) to 0644 and every
    folder to 0755; drop anything that is not a file or folder. → the paths
    that HAD an executable bit."""
    had_exec: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if os.path.realpath(dirpath) == os.path.realpath(root) and ".git" in dirnames:
            dirnames.remove(".git")
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            st = os.lstat(full)
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(full)
                dirnames.remove(d)
                continue
            os.chmod(full, 0o755)
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            st = os.lstat(full)
            if not stat.S_ISREG(st.st_mode):
                os.unlink(full)  # a symlink, fifo, socket or device: never kept
                continue
            if st.st_mode & 0o111:
                had_exec.append(os.path.relpath(full, root).replace(os.sep, "/"))
            if stat.S_IMODE(st.st_mode) != 0o644:
                os.chmod(full, 0o644)
    return sorted(had_exec)


def unpack(
    bundle: str,
    dest: str,
    branch: str,
    *,
    check: Optional[Callable[[List[Tuple[str, int]], int], None]] = None,
) -> dict:
    """Verify ``bundle`` and check its one branch out into the NEW folder
    ``dest`` (it must not exist) → ``{path, branch, head, exec_stripped}``.
    ``check(entries, objects)`` sees the branch's tree BEFORE anything is
    checked out — ``[(path, size)]`` per file and the repository's object
    count — and raises to refuse it (a planted repository layout, too many
    files, a checkout bigger than the space left). Raises
    :class:`BundleError` (``bad_bundle``) or what ``check`` raised; ``dest``
    is removed then."""
    if not valid_branch(branch):
        raise BundleError(
            "bad_bundle", "the bundle names an unusable branch %r" % branch
        )
    if os.path.lexists(dest):
        raise BundleError(
            "exists", "%s already exists — nothing is ever unpacked over it" % dest
        )
    env = _env(isolated=True)
    safe = [
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.symlinks=false",
        "-c",
        "core.fileMode=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.file.allow=always",
    ]
    try:
        _run_ok(["init", "--template=", "-q", dest], env)
        g = ["-C", dest] + safe
        for key, val in (("core.symlinks", "false"), ("core.fileMode", "false")):
            _run_ok(["-C", dest, "config", key, val], env)
        rc, _, err = _run(g + ["bundle", "verify", "-q", bundle], env=env)
        if rc != 0:
            raise BundleError(
                "bad_bundle",
                "the bundle does not verify on its own (%s)"
                % (err.splitlines() or ["?"])[-1],
            )
        rc, out, err = _run(g + ["bundle", "list-heads", bundle], env=env)
        heads = [
            ln.split()
            for ln in out.decode("utf-8", "replace").splitlines()
            if ln.strip()
        ]
        want = "refs/heads/" + branch
        if rc != 0 or len(heads) != 1 or len(heads[0]) != 2 or heads[0][1] != want:
            raise BundleError(
                "bad_bundle",
                "the bundle must carry exactly one branch, %s (it carries %s)"
                % (want, ", ".join(h[-1] for h in heads) or "none"),
            )
        rc, _, err = _run(
            g
            + [
                "-c",
                "fetch.fsckObjects=true",
                "-c",
                "transfer.fsckObjects=true",
                "fetch",
                "-q",
                "--no-tags",
                "--no-write-fetch-head",
                bundle,
                "%s:%s" % (want, want),
            ],
            env=env,
        )
        if rc != 0:
            raise BundleError(
                "bad_bundle",
                "the bundle's objects failed git's checks: %s"
                % ((err.splitlines() or ["?"])[-1]),
            )
        if check is not None:
            check(*_tree_entries(g, want, env))
        rc, _, err = _run(g + ["checkout", "-q", "-f", branch], env=env)
        if rc != 0:
            raise BundleError(
                "bad_bundle",
                "could not check the branch out: %s"
                % ((err.splitlines() or ["?"])[-1]),
            )
        head = _run(g + ["rev-parse", "HEAD"], env=env)[1].decode().strip()
        stripped = _strip_modes(dest)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return {"path": dest, "branch": branch, "head": head, "exec_stripped": stripped}


def _tree_entries(
    g: List[str], ref: str, env: dict
) -> Tuple[List[Tuple[str, int]], int]:
    """``([(path, size)], objects)``: every file a checkout of ``ref`` would
    write (a submodule entry counts 0 bytes) and the objects the repository
    now holds — read before the checkout."""
    rc, out, err = _run(g + ["ls-tree", "-r", "-l", "-z", "--full-tree", ref], env=env)
    if rc != 0:
        raise BundleError("bad_bundle", "could not read the bundle's tree: %s" % err)
    entries: List[Tuple[str, int]] = []
    for rec in out.split(b"\x00"):
        if not rec:
            continue
        meta, _, name = rec.partition(b"\t")
        cols = meta.split()
        size = cols[3] if len(cols) > 3 else b"-"
        entries.append(
            (
                name.decode("utf-8", "surrogateescape"),
                int(size) if size.isdigit() else 0,
            )
        )
    rc, out, _ = _run(g + ["count-objects", "-v"], env=env)
    objects = 0
    for line in out.decode("utf-8", "replace").splitlines():
        key, _, val = line.partition(":")
        if key.strip() in ("count", "in-pack") and val.strip().isdigit():
            objects += int(val.strip())
    return entries, objects


def _run_ok(args: List[str], env: dict) -> None:
    rc, _, err = _run(args, env=env)
    if rc != 0:
        raise BundleError("bad_bundle", "git %s failed: %s" % (args[0], err or rc))
