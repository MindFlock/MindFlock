"""Adversarial tests for backend.peer.share.

The attacker is the sandboxed agent (prompt-injected by the peer) — it owns
``work/`` and can create anything an unprivileged process can there — and
the peer, who picks the paths ``read_file`` is asked for. The invariants:

* nothing outside ``work/`` is ever returned by ``read_file`` (or by diff);
* no host-side git call on a share ever executes a program the agent
  planted (fsmonitor, hooks, filter/textconv drivers, pagers, editors), no
  matter what it wrote in ``work/`` or its ``home/``;
* nothing blocks forever on a FIFO, a device or a huge file;
* export never lands outside ``refs/heads/peer/…`` in the user's repo and
  never runs anything there.

A "canary" is a shell script that records that it ran; every test that
plants one asserts it never ran.
"""

from __future__ import annotations

import os
import stat
import threading
import time

import pytest

from backend.peer import share as sh
from tests.unit.peer.test_share import (  # noqa: F401 — fixtures
    LINK,
    commit_all,
    peer_home,
    repo,
    run_git,
    share,
)

SECRET = "TOP-SECRET-OUTSIDE-CONTENT"


@pytest.fixture
def outside(tmp_path):
    """Host files the agent must never reach."""
    d = tmp_path / "outside"
    d.mkdir()
    (d / "secret.txt").write_text(SECRET + "\n")
    (d / "f").write_text(SECRET + "\n")
    return d


class Canary:
    def __init__(self, base):
        self.mark = base / "CANARY-RAN"
        self.script = base / "canary.sh"
        self.script.write_text(
            "#!/bin/sh\necho \"$0 $*\" >> '%s'\ncat\nexit 0\n" % self.mark
        )
        self.script.chmod(0o755)

    @property
    def ran(self):
        return self.mark.exists()

    def hooks_dir(self, d):
        """A hooks dir where every hook git might run is the canary."""
        d.mkdir(parents=True, exist_ok=True)
        for name in (
            "pre-commit",
            "prepare-commit-msg",
            "commit-msg",
            "post-commit",
            "post-checkout",
            "post-merge",
            "pre-push",
            "post-index-change",
            "reference-transaction",
            "fsmonitor-watchman",
            "pre-auto-gc",
            "post-rewrite",
            "push-to-checkout",
        ):
            p = d / name
            p.write_text("#!/bin/sh\ntouch '%s'\n" % self.mark)
            p.chmod(0o755)
        return d

    def evil_config(self, hooks):
        return (
            "[core]\n"
            "\tfsmonitor = %(s)s\n"
            "\thooksPath = %(h)s\n"
            "\tpager = %(s)s\n"
            "\teditor = %(s)s\n"
            "\tsshCommand = %(s)s\n"
            "\tattributesFile = %(a)s\n"
            '[filter "evil"]\n'
            "\tclean = %(s)s\n"
            "\tsmudge = %(s)s\n"
            "\tprocess = %(s)s\n"
            "\trequired = true\n"
            '[diff "evil"]\n'
            "\ttextconv = %(s)s\n"
            "\tcommand = %(s)s\n"
            "[diff]\n"
            "\texternal = %(s)s\n"
            "[uploadpack]\n"
            "\tpackObjectsHook = %(s)s\n"
            "[alias]\n"
            "\tstatus = !%(s)s\n"
            "\tdiff = !%(s)s\n"
        ) % {"s": self.script, "h": hooks, "a": hooks / "attrs"}


@pytest.fixture
def canary(tmp_path):
    return Canary(tmp_path)


def exercise_all(s):
    """Every host-side read/write path on a share."""
    sh.list_files(s)
    sh.diff(s, 200000)
    for p in ("a.txt", "src/m.py"):
        try:
            sh.read_file(s, p)
        except sh.ShareError:
            pass
    sh.checkpoint(s, "after attack")
    sh.diff(s, 200000)
    sh.git(s, "status", "--porcelain")


def never_returns_secret(s, relpath):
    try:
        got = sh.read_file(s, relpath)
    except sh.ShareError as e:
        assert str(e) == "not found"
        return
    assert SECRET not in got["content"]


# --------------------------------------------------------------------------- #
# Path traversal
# --------------------------------------------------------------------------- #
TRAVERSALS = [
    "../outside/secret.txt",
    "../../outside/secret.txt",
    "a.txt/../../outside/secret.txt",
    "src/../../outside/secret.txt",
    "src/../a.txt",  # even a harmless .. is refused
    "..",
    ".",
    "./",
    "",
    "/",
    "/etc/passwd",
    "//etc/passwd",
    "a.txt/",
    "src//m.py",
    "a.txt\x00.png",
    "\x00",
    ".git",
    ".git/config",
    ".git/HEAD",
    "src/.git/config",
    ".GIT/config",
    ".Git",
    "x/" * 64 + "f",
    "a" * 1025,
    "a" * 256,
]


@pytest.mark.parametrize("relpath", TRAVERSALS)
def test_traversal_refused_uniformly(share, outside, relpath):
    with pytest.raises(sh.ShareError) as ei:
        sh.read_file(share, relpath)
    assert str(ei.value) == "not found"


@pytest.mark.parametrize("relpath", [None, 0, 1.5, b"a.txt", ["a.txt"], {"p": 1}])
def test_non_string_path_refused(share, relpath):
    with pytest.raises(sh.ShareError, match="^not found$"):
        sh.read_file(share, relpath)


def test_lone_surrogate_refused(share):
    with pytest.raises(sh.ShareError, match="^not found$"):
        sh.read_file(share, "a\udcff")


@pytest.mark.parametrize(
    "literal",
    [
        "..\\..\\outside\\secret.txt",  # backslashes are filename characters
        "%2e%2e/%2e%2e/outside/secret.txt",  # no URL decoding
        "‥/outside/secret.txt",  # two-dot leader
        "．．/outside/secret.txt",  # fullwidth dots
        "․․/secret.txt",  # one-dot leaders
        ".​./secret.txt",  # zero-width space
        "..̸/secret.txt",
        "İ.txt",  # dotted capital I (casefold trap)
    ],
)
def test_lookalikes_are_literal_names(share, outside, literal):
    never_returns_secret(share, literal)
    with pytest.raises(sh.ShareError, match="^not found$"):
        sh.read_file(share, literal)
    # ...and a file that really has that literal name is readable as itself
    first = literal.split("/")[0]
    if first not in (".", ".."):
        os.makedirs(os.path.join(share.work, os.path.dirname(literal)), exist_ok=True)
        with open(os.path.join(share.work, literal), "w") as f:
            f.write("literal")
        assert sh.read_file(share, literal)["content"] == "literal"


def test_dot_git_is_case_insensitive_but_lookalikes_are_not(share):
    os.makedirs(os.path.join(share.work, ".GIT"))
    open(os.path.join(share.work, ".GIT", "x"), "w").write("x")
    with pytest.raises(sh.ShareError):
        sh.read_file(share, ".GIT/x")  # refused (fail closed on any case)
    assert ".GIT/x" not in sh.list_files(share)["files"]
    os.makedirs(os.path.join(share.work, ".gitx"))
    open(os.path.join(share.work, ".gitx", "y"), "w").write("y")
    assert sh.read_file(share, ".gitx/y")["content"] == "y"


def test_gitfile_and_gitdir_unreadable(share):
    for p in (".git", "./.git", "src/../.git"):
        with pytest.raises(sh.ShareError):
            sh.read_file(share, p)
    assert ".git" not in sh.list_files(share)["files"]


# --------------------------------------------------------------------------- #
# Symlinks
# --------------------------------------------------------------------------- #
def test_symlink_leaf_to_outside_file(share, outside):
    os.symlink(outside / "secret.txt", os.path.join(share.work, "leak"))
    os.symlink("../../../../outside/secret.txt", os.path.join(share.work, "rel"))
    for p in ("leak", "rel"):
        with pytest.raises(sh.ShareError, match="^not found$"):
            sh.read_file(share, p)


def test_symlink_leaf_to_inside_file_also_refused(share):
    os.symlink("a.txt", os.path.join(share.work, "alias"))
    with pytest.raises(sh.ShareError):
        sh.read_file(share, "alias")


def test_symlink_intermediate_dir(share, outside):
    os.symlink(outside, os.path.join(share.work, "dirlink"))
    os.makedirs(os.path.join(share.work, "real"))
    os.symlink(outside, os.path.join(share.work, "real", "deep"))
    os.symlink("/", os.path.join(share.work, "root"))
    for p in (
        "dirlink/secret.txt",
        "real/deep/secret.txt",
        "root/etc/passwd",
        "dirlink",
        "root",
    ):
        with pytest.raises(sh.ShareError, match="^not found$"):
            sh.read_file(share, p)


def test_symlink_to_proc_and_dev(share):
    for name, target in (
        ("p", "/proc/self/root/etc/passwd"),
        ("z", "/dev/zero"),
        ("t", "/dev/tty"),
        ("e", "/proc/self/environ"),
    ):
        os.symlink(target, os.path.join(share.work, name))
        with pytest.raises(sh.ShareError):
            sh.read_file(share, name)


def test_symlinked_work_dir_itself_refused(share, outside):
    """If work/ were swapped for a symlink (it can't be from the sandbox —
    its parent is not writable there — but host code must not care)."""
    os.rename(share.work, share.work + ".bak")
    os.symlink(outside, share.work)
    with pytest.raises(sh.ShareError):
        sh.read_file(share, "secret.txt")


def test_symlinks_in_diff_and_list_are_not_followed(share, outside):
    os.symlink(outside / "secret.txt", os.path.join(share.work, "leak"))
    os.unlink(os.path.join(share.work, "a.txt"))
    os.symlink(outside / "secret.txt", os.path.join(share.work, "a.txt"))
    d = sh.diff(share, 200000)
    assert SECRET not in d["diff"]
    sh.list_files(share)
    sha = sh.checkpoint(share, "links")
    # git stores the link itself, never the target's content
    blob = sh.git(share, "cat-file", "-p", sha + ":leak").stdout.decode()
    assert blob == str(outside / "secret.txt")
    assert SECRET not in sh.git(share, "show", sha).stdout.decode()


def _flipper(stop, swap):
    while not stop.is_set():
        try:
            swap()
        except OSError:
            pass


def test_race_intermediate_dir_swapped_for_symlink(share, outside):
    """A thread keeps flipping work/d between a real dir and a symlink to the
    outside while read_file loops: it must never return outside content."""
    w = share.work
    os.makedirs(os.path.join(w, "d_real"))
    open(os.path.join(w, "d_real", "f"), "w").write("inside\n")
    os.symlink(outside, os.path.join(w, "d_link"))
    os.rename(os.path.join(w, "d_real"), os.path.join(w, "d"))
    state = {"real": True}

    def swap():
        d = os.path.join(w, "d")
        if state["real"]:
            os.rename(d, os.path.join(w, "d_real"))
            os.rename(os.path.join(w, "d_link"), d)
        else:
            os.rename(d, os.path.join(w, "d_link"))
            os.rename(os.path.join(w, "d_real"), d)
        state["real"] = not state["real"]

    stop = threading.Event()
    t = threading.Thread(target=_flipper, args=(stop, swap), daemon=True)
    t.start()
    seen = {"inside": 0, "refused": 0}
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                got = sh.read_file(share, "d/f")
            except sh.ShareError:
                seen["refused"] += 1
                continue
            assert got["content"] == "inside\n", got
            seen["inside"] += 1
    finally:
        stop.set()
        t.join(5)
    assert seen["inside"] + seen["refused"] > 100


def test_race_leaf_swapped_for_symlink(share, outside):
    w = share.work
    leaf = os.path.join(w, "f")
    reg = os.path.join(w, "f.reg")
    lnk = os.path.join(w, "f.lnk")
    open(leaf, "w").write("inside\n")

    def swap():
        # atomic replace of the leaf with either a symlink or a regular file
        if os.path.islink(leaf):
            open(reg, "w").write("inside\n")
            os.replace(reg, leaf)
        else:
            os.symlink(outside / "secret.txt", lnk)
            os.replace(lnk, leaf)

    stop = threading.Event()
    t = threading.Thread(target=_flipper, args=(stop, swap), daemon=True)
    t.start()
    n = 0
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                got = sh.read_file(share, "f")
            except sh.ShareError:
                continue
            n += 1
            assert got["content"] == "inside\n"
            assert SECRET not in sh.diff(share, 5000)["diff"]
    finally:
        stop.set()
        t.join(5)
    assert n > 0


# --------------------------------------------------------------------------- #
# Hardlinks, FIFOs, devices, sockets, directories, huge files
# --------------------------------------------------------------------------- #
def test_hardlink_to_outside_file_refused(share, outside):
    """Inside the sandbox the agent can't see host files to hardlink them,
    but read_file still refuses any file with st_nlink > 1."""
    try:
        os.link(outside / "secret.txt", os.path.join(share.work, "hl"))
    except OSError as e:  # e.g. protected_hardlinks / cross-device
        pytest.skip("cannot hardlink here: %s" % e)
    with pytest.raises(sh.ShareError, match="^not found$"):
        sh.read_file(share, "hl")
    d = sh.diff(share, 200000)
    assert SECRET not in d["diff"]


def test_hardlink_inside_also_refused(share):
    os.link(os.path.join(share.work, "a.txt"), os.path.join(share.work, "a2"))
    for p in ("a.txt", "a2"):
        with pytest.raises(sh.ShareError):
            sh.read_file(share, p)


def _within(seconds, fn, *args):
    out = {}

    def run():
        try:
            out["v"] = fn(*args)
        except BaseException as e:  # noqa: BLE001
            out["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "%s blocked" % getattr(fn, "__name__", fn)
    return out


def test_fifo_never_blocks(share):
    os.mkfifo(os.path.join(share.work, "pipe"))
    out = _within(5, sh.read_file, share, "pipe")
    assert isinstance(out.get("e"), sh.ShareError)
    # a tracked file replaced by a FIFO
    os.unlink(os.path.join(share.work, "a.txt"))
    os.mkfifo(os.path.join(share.work, "a.txt"))
    for fn, args in (
        (sh.list_files, (share,)),
        (sh.diff, (share, 10000)),
        (sh.checkpoint, (share, "fifo")),
    ):
        out = _within(30, fn, *args)
        assert "e" not in out or isinstance(out["e"], sh.ShareError), out


def test_device_and_socket_files_refused(share, tmp_path):
    import socket as _s

    # The agent can't mknod (no CAP_MKNOD); the leaf check still refuses
    # devices — show it on real device inodes.
    for dev in ("/dev/null", "/dev/zero"):
        with pytest.raises(sh.ShareError):
            sh._check_leaf(os.stat(dev))
    sock_path = os.path.join(share.work, "s.sock")
    if len(sock_path) < 100:
        s = _s.socket(_s.AF_UNIX)
        s.bind(sock_path)
        try:
            out = _within(5, sh.read_file, share, "s.sock")
            assert isinstance(out.get("e"), sh.ShareError)
        finally:
            s.close()


def test_open_never_opens_non_regular_leaf(share, monkeypatch):
    """The leaf is opened O_PATH first and only reopened for reading once
    fstat proved it a regular file — a FIFO/device is never open(2)ed."""
    opened = []
    real = os.open

    def spy(path, flags, *a, **kw):
        opened.append((path, flags))
        return real(path, flags, *a, **kw)

    os.mkfifo(os.path.join(share.work, "pipe"))
    monkeypatch.setattr(sh.os, "open", spy)
    with pytest.raises(sh.ShareError):
        sh.read_file(share, "pipe")
    leaf_opens = [f for p, f in opened if p == "pipe"]
    assert leaf_opens and all(f & os.O_PATH for f in leaf_opens)
    assert not any(str(p).startswith("/proc/self/fd/") for p, _ in opened)


def test_directory_refused(share):
    for p in ("src", "src/."):
        with pytest.raises(sh.ShareError, match="^not found$"):
            sh.read_file(share, p)


def test_huge_sparse_file_is_capped(share):
    p = os.path.join(share.work, "huge.bin")
    with open(p, "wb") as f:
        f.truncate(4 * 1024**3)  # 4 GiB sparse
    t0 = time.monotonic()
    got = sh.read_file(share, "huge.bin")
    assert time.monotonic() - t0 < 5
    assert got["truncated"] is True
    assert got["size"] == 4 * 1024**3
    import base64

    assert len(base64.b64decode(got["content"])) == sh.READ_MAX_BYTES
    got = sh.read_file(share, "huge.bin", max_bytes=10)
    assert len(base64.b64decode(got["content"])) == 10
    d = sh.diff(share, 5000)
    assert len(d["diff"]) <= 5000
    assert any(r["path"] == "huge.bin" for r in d["stat"])


def test_unreadable_file_refused(share):
    p = os.path.join(share.work, "noperm")
    open(p, "w").write("x")
    os.chmod(p, 0)
    if os.access(p, os.R_OK):
        pytest.skip("running as root")
    with pytest.raises(sh.ShareError, match="^not found$"):
        sh.read_file(share, "noperm")


def test_error_messages_never_differ(share, outside):
    """No oracle: missing, symlink, dir, fifo, traversal, .git — one message."""
    os.symlink(outside / "secret.txt", os.path.join(share.work, "leak"))
    os.mkfifo(os.path.join(share.work, "pipe"))
    msgs = set()
    for p in (
        "missing",
        "leak",
        "src",
        "pipe",
        "../x",
        ".git/config",
        "leak/x",
        "a.txt/x",
    ):
        with pytest.raises(sh.ShareError) as ei:
            sh.read_file(share, p)
        msgs.add(str(ei.value))
    assert msgs == {"not found"}


# --------------------------------------------------------------------------- #
# Planted git config / hooks / drivers — nothing may execute
# --------------------------------------------------------------------------- #
def test_replaced_dot_git_dir_is_ignored(share, canary, tmp_path):
    """The agent replaces the work/.git gitfile with a directory holding a
    config full of executables (in production work/.git is a read-only bind,
    but host git must not care: GIT_DIR is explicit)."""
    gf = os.path.join(share.work, ".git")
    os.chmod(gf, 0o600)
    os.unlink(gf)
    evil = os.path.join(gf)
    os.makedirs(os.path.join(evil, "objects"))
    os.makedirs(os.path.join(evil, "refs", "heads"))
    open(os.path.join(evil, "HEAD"), "w").write("ref: refs/heads/main\n")
    hooks = canary.hooks_dir(tmp_path / "evilhooks")
    canary.hooks_dir(tmp_path / "evilgit-hooks")
    open(os.path.join(evil, "config"), "w").write(canary.evil_config(hooks))
    canary.hooks_dir(type(tmp_path)(evil) / "hooks")
    open(os.path.join(share.work, ".gitattributes"), "w").write(
        "* filter=evil diff=evil merge=evil\n"
    )
    open(os.path.join(share.work, "a.txt"), "a").write("changed\n")
    exercise_all(share)
    assert not canary.ran
    # the commit went to the TRUSTED git dir, not the planted one
    assert os.listdir(os.path.join(evil, "objects")) == []
    log = sh.git(share, "log", "-1", "--format=%s").stdout.decode().strip()
    assert log == "after attack"


def test_gitfile_pointing_at_evil_gitdir_is_ignored(share, canary, tmp_path):
    evilgit = tmp_path / "evil.git"
    run_git(tmp_path, "init", "-q", "--bare", str(evilgit))
    hooks = canary.hooks_dir(tmp_path / "evilhooks")
    with open(evilgit / "config", "a") as f:
        f.write(canary.evil_config(hooks))
    gf = os.path.join(share.work, ".git")
    os.chmod(gf, 0o600)
    open(gf, "w").write("gitdir: %s\n" % evilgit)
    open(os.path.join(share.work, "new"), "w").write("n")
    exercise_all(share)
    assert not canary.ran
    assert run_git(evilgit, "rev-list", "--all").stdout == ""


def test_nested_repo_with_evil_config(share, canary, tmp_path):
    """A nested repo with an fsmonitor/hook-laden config: checkpoint never
    commits it as a gitlink and nothing in it runs."""
    sub = os.path.join(share.work, "sub")
    os.makedirs(sub)
    run_git(sub, "init", "-q")
    open(os.path.join(sub, "inner.txt"), "w").write("inner\n")
    run_git(sub, "add", ".")
    run_git(sub, "commit", "-qm", "inner")
    hooks = canary.hooks_dir(tmp_path / "subhooks")
    with open(os.path.join(sub, ".git", "config"), "a") as f:
        f.write(canary.evil_config(hooks))
    canary.hooks_dir(type(tmp_path)(sub) / ".git" / "hooks")
    open(os.path.join(share.work, ".gitmodules"), "w").write(
        '[submodule "sub"]\n\tpath = sub\n\turl = ext::%s\n\tupdate = !%s\n'
        % (canary.script, canary.script)
    )
    exercise_all(share)
    sha = sh.checkpoint(share, "again")
    tree = sh.git(share, "ls-tree", "-r", sha).stdout.decode()
    assert "160000" not in tree
    assert "sub/inner.txt" not in tree  # nested repos are skipped entirely
    assert not canary.ran
    assert "sub/" not in sh.list_files(share)["files"]


def test_gitlink_from_source_repo_is_stripped(peer_home, repo, canary):
    """A submodule entry already in the user's repo never survives a
    checkpoint as a gitlink."""
    sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    run_git(repo, "update-index", "--add", "--cacheinfo", "160000,%s,vendor" % sha)
    (repo / ".gitmodules").write_text(
        '[submodule "vendor"]\n\tpath = vendor\n\turl = ext::%s\n' % canary.script
    )
    run_git(repo, "add", ".gitmodules")
    run_git(repo, "commit", "-qm", "with submodule")  # not add -A: keep the gitlink
    s = sh.create_share(LINK, str(repo))
    # stripped at creation: neither the index nor the base commit has it
    assert "160000" not in sh.git(s, "ls-files", "-s").stdout.decode()
    assert "160000" not in sh.git(s, "ls-tree", "-r", sh.BASE_REF).stdout.decode()
    open(os.path.join(s.work, "x"), "w").write("x")
    new = sh.checkpoint(s, "strip")
    assert "160000" not in sh.git(s, "ls-tree", "-r", new).stdout.decode()
    assert "160000" not in sh.git(s, "ls-files", "-s").stdout.decode()
    assert not canary.ran


def test_gitattributes_filter_with_evil_global_config(
    share, canary, tmp_path, monkeypatch
):
    """``.gitattributes`` names filter/diff drivers; the host user's global
    (and XDG and system-ish) config DEFINES them. GIT_CONFIG_GLOBAL=/dev/null
    and the minimal env must keep every one of them from running."""
    hooks = canary.hooks_dir(tmp_path / "globalhooks")
    evil_cfg = tmp_path / "evil.gitconfig"
    evil_cfg.write_text(canary.evil_config(hooks))
    xdg = tmp_path / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "config").write_text(canary.evil_config(hooks))
    fakehome = tmp_path / "fakehome"
    fakehome.mkdir()
    (fakehome / ".gitconfig").write_text(canary.evil_config(hooks))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(evil_cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(evil_cfg))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(fakehome))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(canary.script))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hookspath'='%s'" % hooks)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(canary.script))
    monkeypatch.setenv("GIT_PAGER", str(canary.script))
    monkeypatch.setenv("GIT_SSH_COMMAND", str(canary.script))
    open(os.path.join(share.work, ".gitattributes"), "w").write(
        "* filter=evil diff=evil merge=evil text\n"
    )
    open(os.path.join(share.work, "a.txt"), "a").write("more\n")
    open(os.path.join(share.work, "untracked.txt"), "w").write("u\n")
    exercise_all(share)
    assert not canary.ran
    # the committed content is the raw bytes (no clean filter ran)
    assert sh.git(share, "show", "HEAD:a.txt").stdout.endswith(b"more\n")


def test_agent_writable_home_config_is_ignored(share, canary, tmp_path):
    """HOME for host git is the agent's own (writable) home: every config,
    attributes and ignore file it can plant there must be ignored."""
    hooks = canary.hooks_dir(tmp_path / "homehooks")
    home = share.home
    open(os.path.join(home, ".gitconfig"), "w").write(canary.evil_config(hooks))
    os.makedirs(os.path.join(home, ".config", "git"))
    open(os.path.join(home, ".config", "git", "config"), "w").write(
        canary.evil_config(hooks)
    )
    open(os.path.join(home, ".config", "git", "attributes"), "w").write(
        "* filter=evil diff=evil\n"
    )
    open(os.path.join(home, ".config", "git", "ignore"), "w").write("*\n")
    open(os.path.join(share.work, "a.txt"), "a").write("x\n")
    open(os.path.join(share.work, "seen.txt"), "w").write("x\n")
    exercise_all(share)
    assert not canary.ran
    # the planted global ignore did not hide files either
    assert "seen.txt" in sh.git(share, "ls-files").stdout.decode()


def test_hooks_path_tricks(share, canary, tmp_path):
    """Hooks can't run: core.hooksPath is forced to /dev/null per call, the
    configured no-hooks dir is empty and read-only, and repo.git/hooks is
    empty. Even with hooks planted in all three places (the agent can't in
    production — they are read-only binds), checkpoint runs none."""
    canary.hooks_dir(type(tmp_path)(share.gitdir) / "hooks")
    nohooks = os.path.join(share.run, "no-hooks")
    os.chmod(nohooks, 0o700)
    canary.hooks_dir(type(tmp_path)(nohooks))
    hooks_in_work = canary.hooks_dir(type(tmp_path)(share.work) / ".githooks")
    assert hooks_in_work
    open(os.path.join(share.work, "f"), "w").write("f")
    sh.checkpoint(share, "hooks?")
    sh.diff(share, 1000)
    assert not canary.ran


def test_no_hooks_dir_is_readonly_and_empty(share):
    nohooks = os.path.join(share.run, "no-hooks")
    assert os.listdir(nohooks) == []
    assert stat.S_IMODE(os.stat(nohooks).st_mode) & 0o222 == 0


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
BAD_BRANCHES = [
    "--upload-pack=touch /tmp/pwned",
    "-x",
    "-",
    "main",
    "refs/heads/main",
    "refs/heads/peer/x",
    "peer/../main",
    "peer/..",
    "peer/",
    "peer",
    "peer//x",
    "peer/x.lock",
    "peer/a b",
    "peer/a~1",
    "peer/a^",
    "peer/a:b",
    "peer/a?",
    "peer/a*",
    "peer/[x",
    "peer/a\\b",
    "peer/@{-1}",
    "peer/x@{0}",
    "peer/.hidden",
    "peer/x.",
    "peer/x\x00y",
    "peer/x\ny",
    "peer/x\x1b",
    "peer/" + "x" * 300,
    "PEER/x",
    " peer/x",
    None,
    123,
]


@pytest.mark.parametrize("branch", BAD_BRANCHES)
def test_export_branch_injection_refused(share, repo, branch, tmp_path):
    before = run_git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout
    with pytest.raises(sh.ShareError):
        sh.export(share, str(repo), branch)
    after = run_git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout
    assert before == after
    assert not os.path.exists("/tmp/pwned")


def test_export_runs_nothing_in_target(share, repo, canary, tmp_path):
    """Share content that LOOKS like hooks/config/filters, plus the target's
    own hooks: the fetch checks nothing out and runs no hook."""
    w = share.work
    os.makedirs(os.path.join(w, "hooks"))
    open(os.path.join(w, "hooks", "post-checkout"), "w").write(
        "#!/bin/sh\ntouch %s\n" % canary.mark
    )
    open(os.path.join(w, ".gitattributes"), "w").write("* filter=evil\n")
    open(os.path.join(w, ".gitmodules"), "w").write(
        '[submodule "x"]\n\tpath = x\n\turl = ext::%s\n' % canary.script
    )
    canary.hooks_dir(repo / ".git" / "hooks")
    with open(repo / ".git" / "config", "a") as f:
        f.write(
            '[filter "evil"]\n\tclean = %s\n\tsmudge = %s\n'
            % (canary.script, canary.script)
        )
    head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    out = sh.export(share, str(repo), "peer/safe")
    assert not canary.ran
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert run_git(repo, "status", "--porcelain").stdout == ""
    assert not (repo / "hooks").exists()
    refs = run_git(repo, "for-each-ref", "--format=%(refname)").stdout.split()
    assert "refs/heads/peer/safe" in refs
    assert all(
        r in ("refs/heads/main", "refs/heads/feature", "refs/heads/peer/safe")
        for r in refs
    )
    assert run_git(repo, "rev-parse", "peer/safe").stdout.strip() == out["sha"]


def test_export_refuses_target_inside_peer_root(share, peer_home, repo):
    for target in (
        share.work,
        share.root,
        share.gitdir,
        str(peer_home),
        os.path.join(share.work, "src"),
    ):
        with pytest.raises(sh.ShareError):
            sh.export(share, target, "peer/x")


def test_export_refuses_symlink_to_share(share, tmp_path):
    link = tmp_path / "innocent"
    os.symlink(share.work, link)
    with pytest.raises(sh.ShareError, match="inside the peer"):
        sh.export(share, str(link), "peer/x")


def test_export_does_not_touch_target_head_branch(share, repo):
    run_git(repo, "branch", "peer/existing")
    old = run_git(repo, "rev-parse", "main").stdout.strip()
    open(os.path.join(share.work, "z"), "w").write("z")
    sh.export(share, str(repo), "peer/existing")
    assert run_git(repo, "rev-parse", "main").stdout.strip() == old


# --------------------------------------------------------------------------- #
# remove_share containment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "..",
        "../..",
        "/",
        "ABCDEF0123456789",
        "ab" * 40,
        "a" * 15,
        "ab/cd" * 4,
        None,
        7,
    ],
)
def test_remove_share_bad_ids(peer_home, bad):
    with pytest.raises((sh.ShareError, ValueError)):
        sh.remove_share(bad)


def test_remove_share_refuses_symlinked_root(peer_home, outside):
    shares = peer_home / "shares"
    shares.mkdir(parents=True)
    sid = "cd" * 16
    os.symlink(outside, shares / sid)
    with pytest.raises(sh.ShareError):
        sh.remove_share(sid)
    assert (outside / "secret.txt").read_text().startswith(SECRET)


def test_remove_share_does_not_follow_inner_symlinks(share, outside):
    os.symlink(outside, os.path.join(share.work, "out"))
    os.symlink(outside / "secret.txt", os.path.join(share.home, "s"))
    d = os.path.join(share.work, "locked")
    os.makedirs(d)
    os.symlink(outside, os.path.join(d, "o"))
    os.chmod(d, 0)
    assert sh.remove_share(share.share_id)
    assert not os.path.exists(share.root)
    assert (outside / "secret.txt").read_text().startswith(SECRET)
    assert stat.S_IMODE(os.stat(outside).st_mode) != 0


def test_remove_share_is_running_callable_errors_propagate(share):
    def boom(sid):
        raise RuntimeError("engine down")

    with pytest.raises(RuntimeError):
        sh.remove_share(share.share_id, is_running=boom)
    assert os.path.isdir(share.root)  # fail closed


def test_clone_runs_no_hooks_or_filters_from_source(
    peer_home, tmp_path, canary, monkeypatch
):
    """create_share clones the user's repo with an empty template, hooks off
    and no global config: a filter/hook the source repo's config or the
    user's global config defines never runs during the clone."""
    r = tmp_path / "src-repo"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    (r / ".gitattributes").write_text("* filter=evil\n")
    (r / "f").write_text("f\n")
    commit_all(r)
    canary.hooks_dir(r / ".git" / "hooks")
    with open(r / ".git" / "config", "a") as f:
        f.write(
            '[filter "evil"]\n\tsmudge = %s\n\tclean = %s\n'
            % (canary.script, canary.script)
        )
    g = tmp_path / "g.gitconfig"
    g.write_text(canary.evil_config(canary.hooks_dir(tmp_path / "gh")))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(g))
    s = sh.create_share(LINK, str(r))
    assert not canary.ran
    assert open(os.path.join(s.work, "f")).read() == "f\n"
    exercise_all(s)
    assert not canary.ran


def test_list_and_diff_never_include_git_internals(share):
    os.makedirs(os.path.join(share.work, "x", ".git"))
    open(os.path.join(share.work, "x", ".git", "config"), "w").write("c")
    open(os.path.join(share.work, "x", "y"), "w").write("y")
    files = sh.list_files(share)["files"]
    assert not any(".git/" in f or f.endswith(".git") for f in files)
    d = sh.diff(share, 100000)
    assert all(".git/" not in r["path"] for r in d["stat"])


def test_subprocess_never_inherits_fds(share):
    """git children get close_fds: an fd the host holds is not leaked into
    a git process the share's content could influence."""
    r, w = os.pipe()
    try:
        res = sh._run(
            ["sh", "-c", "ls /proc/self/fd"],
            env={"PATH": os.environ["PATH"]},
            cwd="/",
            timeout=10,
        )
        fds = set(res.stdout.decode().split())
        assert str(r) not in fds or r < 3
        assert str(w) not in fds or w < 3
    finally:
        os.close(r)
        os.close(w)


def test_git_subprocess_has_no_stdin(share):
    res = sh._run(
        ["sh", "-c", "cat; echo done"],
        env={"PATH": os.environ["PATH"]},
        cwd="/",
        timeout=5,
    )
    assert res.stdout == b"done\n"


def test_shell_metachars_in_paths_are_inert(share, canary):
    name = "$(touch pwned1);`touch pwned2`;|touch pwned3&.txt"
    with open(os.path.join(share.work, name), "w") as f:
        f.write("x")
    exercise_all(share)
    assert sh.read_file(share, name)["content"] == "x"
    assert name in sh.git(share, "ls-files", "-z").stdout.decode().split("\0")
    for p in ("pwned1", "pwned2", "pwned3"):
        assert not os.path.exists(os.path.join(share.work, p))
        assert not os.path.exists(p)


def test_canary_positive_control(tmp_path, canary):
    """The canaries above are live: ordinary git (no hardened env) on the
    same planted config DOES run them — so 'not canary.ran' means something."""
    r = tmp_path / "ctl"
    r.mkdir()
    run_git(r, "init", "-q")
    (r / "f").write_text("f\n")
    hooks = canary.hooks_dir(tmp_path / "ctlhooks")
    with open(r / ".git" / "config", "a") as f:
        f.write(canary.evil_config(hooks))
    run_git(r, "status", "--porcelain", check=False)
    assert canary.ran
    canary.mark.unlink()
    (r / ".gitattributes").write_text("* filter=evil\n")
    run_git(r, "add", "-A", check=False)
    assert canary.ran


def test_race_flipper_positive_control(share, outside):
    """Without O_NOFOLLOW (a plain open), the swapped-in symlink DOES leak —
    the race tests exercise a real window."""
    os.symlink(outside, os.path.join(share.work, "d"))
    with open(os.path.join(share.work, "d", "f")) as f:
        assert SECRET in f.read()
    with pytest.raises(sh.ShareError):
        sh.read_file(share, "d/f")


def test_engine_git_ignores_planted_attribute_drivers(share, canary, tmp_path):
    """The ENGINE's ordinary git calls (diff stats, status polling) don't go
    through share.git: they run plain ``git -C work`` with the user's own
    global config. If that config defines a driver (git-lfs, a textconv
    tool), a planted ``.gitattributes`` must still not be able to name it —
    repo.git/info/attributes overrides the folder's attributes."""
    hooks = canary.hooks_dir(tmp_path / "unused-hooks")
    user_cfg = tmp_path / "user.gitconfig"
    user_cfg.write_text(
        '[filter "evil"]\n\tclean = %(c)s\n\tsmudge = %(c)s\n\trequired = true\n'
        '[diff "evil"]\n\ttextconv = %(c)s\n\tcommand = %(c)s\n'
        '[merge "evil"]\n\tdriver = %(c)s\n' % {"c": canary.script}
    )
    del hooks
    open(os.path.join(share.work, ".gitattributes"), "w").write(
        "* filter=evil diff=evil merge=evil\n"
    )
    open(os.path.join(share.work, "a.txt"), "a").write("more\n")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": str(user_cfg),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    import subprocess

    for args in (
        ["status", "--porcelain"],
        ["diff", "HEAD"],
        ["diff", "--stat", "HEAD"],
        ["diff", "--numstat", "HEAD"],
        ["add", "-A", "--dry-run"],
        ["log", "-p", "-1"],
    ):
        subprocess.run(
            ["git", "-C", share.work, *args], env=env, capture_output=True, timeout=30
        )
    assert not canary.ran
    attrs = subprocess.run(
        [
            "git",
            "-C",
            share.work,
            "check-attr",
            "filter",
            "diff",
            "merge",
            "--",
            "a.txt",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    assert "evil" not in attrs
