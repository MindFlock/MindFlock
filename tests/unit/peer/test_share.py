"""backend.peer.share — the shared folder: create, read, diff, commit, export.

Functional behaviour. The adversarial cases (symlinks, races, planted git
config, branch injection, …) live in ``test_share_attacks.py``; it imports
the fixtures defined here.
"""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

from backend.peer import paths
from backend.peer import share as sh

LINK = "ab" * 16


def run_git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
    )


def commit_all(repo, msg="c"):
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", msg)


@pytest.fixture
def peer_home(tmp_path, monkeypatch):
    home = tmp_path / "peer"
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(home))
    return home


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("alpha\n")
    (r / "src").mkdir()
    (r / "src" / "m.py").write_text("x = 1\ny = 2\n")
    (r / ".gitignore").write_text("*.log\n")
    commit_all(r, "first")
    (r / "a.txt").write_text("alpha\nbeta\n")
    commit_all(r, "second")
    run_git(r, "branch", "feature")
    return r


@pytest.fixture
def share(peer_home, repo):
    return sh.create_share(LINK, str(repo))


# --------------------------------------------------------------------------- #
# create_share
# --------------------------------------------------------------------------- #
def mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_create_layout_and_permissions(share, peer_home):
    assert paths.SHARE_ID_RE.match(share.share_id)
    assert len(share.share_id) == 32
    assert share.root == os.path.join(
        str(peer_home.resolve()), "shares", share.share_id
    )
    for d in (share.root, share.work, share.gitdir, share.home, share.run):
        assert os.path.isdir(d) and not os.path.islink(d)
        assert mode(d) == 0o700, d
    nohooks = os.path.join(share.run, "no-hooks")
    assert os.listdir(nohooks) == []
    assert mode(nohooks) == 0o500
    # work/.git is a gitfile with the ABSOLUTE git dir
    gitfile = os.path.join(share.work, ".git")
    assert os.path.isfile(gitfile)
    assert open(gitfile).read() == "gitdir: %s\n" % share.gitdir
    assert os.path.isabs(share.gitdir)
    assert sorted(os.listdir(share.work)) == [".git", ".gitignore", "a.txt", "src"]


def test_create_hardened_config(share):
    cfg = os.path.join(share.gitdir, "config")
    out = run_git(share.root, "config", "--file", cfg, "--list").stdout
    got = dict(line.split("=", 1) for line in out.splitlines())
    assert got["core.hookspath"] == os.path.join(share.run, "no-hooks")
    assert got["core.fsmonitor"] == "false"
    assert got["core.untrackedcache"] == "false"
    assert got["diff.ignoresubmodules"] == "all"
    assert got["status.submodulesummary"] == "false"
    assert got["submodule.recurse"] == "false"
    assert got["protocol.allow"] == "never"
    assert got["receive.denycurrentbranch"] == "refuse"
    assert not any(k.startswith(("remote.", "branch.")) for k in got)
    assert "core.worktree" not in got


def test_create_no_hooks_no_remotes_no_info(share):
    assert os.listdir(os.path.join(share.gitdir, "hooks")) == []
    info = os.path.join(share.gitdir, "info")
    assert set(os.listdir(info)) <= {"exclude", "attributes"}
    with open(os.path.join(info, "attributes")) as f:
        assert "* !filter !diff !merge" in f.read()
    assert sh.git(share, "remote").stdout == b""
    refs = sh.git(share, "for-each-ref", "--format=%(refname)").stdout.decode()
    assert "refs/remotes/" not in refs
    assert set(refs.split()) == {"refs/heads/main", sh.BASE_REF}


def test_create_is_shallow_single_commit(share, repo):
    assert os.path.exists(os.path.join(share.gitdir, "shallow"))
    log = sh.git(share, "log", "--format=%s").stdout.decode().split()
    assert log == ["second"]
    head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert sh.git(share, "rev-parse", "HEAD").stdout.decode().strip() == head


def test_create_branch(peer_home, repo):
    (repo / "a.txt").write_text("on main only\n")
    commit_all(repo, "third")
    s = sh.create_share(LINK, str(repo), branch="feature")
    assert open(os.path.join(s.work, "a.txt")).read() == "alpha\nbeta\n"
    assert sh.git(s, "branch", "--show-current").stdout.decode().strip() == "feature"


@pytest.mark.parametrize(
    "branch", ["-x", "--upload-pack=touch /tmp/pwn", "a..b", "no such", "nope"]
)
def test_create_bad_branch_refused_and_cleaned(peer_home, repo, branch):
    with pytest.raises(sh.ShareError):
        sh.create_share(LINK, str(repo), branch=branch)
    shares = peer_home / "shares"
    assert not shares.exists() or list(shares.iterdir()) == []


@pytest.mark.parametrize("link", ["", "XYZ", "ab" * 40, "../x", None])
def test_create_bad_link_id(peer_home, repo, link):
    with pytest.raises(sh.ShareError):
        sh.create_share(link, str(repo))


def test_create_refuses_repo_inside_peer_root(peer_home, repo):
    s = sh.create_share(LINK, str(repo))
    with pytest.raises(sh.ShareError):
        sh.create_share(LINK, s.work)
    with pytest.raises(sh.ShareError):
        sh.create_share(LINK, str(peer_home))


def test_create_missing_repo(peer_home, tmp_path):
    with pytest.raises(sh.ShareError):
        sh.create_share(LINK, str(tmp_path / "nope"))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(sh.ShareError):
        sh.create_share(LINK, str(plain))
    shares = paths.shares_dir()
    assert not os.path.exists(shares) or os.listdir(shares) == []


def test_create_repo_path_with_spaces(peer_home, tmp_path):
    r = tmp_path / "my repo #1"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    (r / "f").write_text("x\n")
    commit_all(r)
    s = sh.create_share(LINK, str(r))
    assert open(os.path.join(s.work, "f")).read() == "x\n"


def test_open_share(share):
    again = sh.open_share(share.share_id)
    assert again == share
    with pytest.raises(sh.ShareError):
        sh.open_share("ff" * 16)
    with pytest.raises(sh.ShareError):
        sh.open_share("../etc")


# --------------------------------------------------------------------------- #
# git()
# --------------------------------------------------------------------------- #
def test_git_env_is_minimal_and_pinned(share, monkeypatch):
    seen = {}
    real = sh._run

    def spy(argv, **kw):
        seen["argv"], seen["env"], seen["cwd"] = argv, kw["env"], kw["cwd"]
        return real(argv, **kw)

    monkeypatch.setenv("GIT_DIR", "/tmp/elsewhere")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.fsmonitor'='evil'")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setattr(sh, "_run", spy)
    sh.git(share, "status", "--porcelain")
    env = seen["env"]
    assert env["GIT_DIR"] == share.gitdir
    assert env["GIT_WORK_TREE"] == share.work
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["HOME"] == share.home
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert "SSH_AUTH_SOCK" not in env
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "PAGER"}
    assert all(k in allowed or k.startswith(("GIT_", "SSH_ASKPASS")) for k in env)
    assert seen["cwd"] == share.work
    argv = seen["argv"]
    assert argv[0] == "git"
    assert "core.hooksPath=/dev/null" in argv and "core.fsmonitor=false" in argv


def test_git_check_raises_without_stderr_leak(share):
    with pytest.raises(sh.ShareError) as ei:
        sh.git(share, "rev-parse", "--verify", "nope-not-a-ref")
    assert str(ei.value) == "git rev-parse failed"
    res = sh.git(share, "rev-parse", "--verify", "nope", check=False)
    assert res.returncode != 0


def test_git_output_cap(share):
    (open(os.path.join(share.work, "big.txt"), "w")).write("z\n" * 100000)
    res = sh.git(
        share,
        "diff",
        "--no-index",
        "/dev/null",
        "big.txt",
        check=False,
        max_output=1000,
    )
    assert res.truncated and len(res.stdout) == 1000


def test_git_timeout(share, monkeypatch):
    with pytest.raises(sh.ShareError, match="timed out"):
        sh._run(["sleep", "5"], env={"PATH": os.environ["PATH"]}, cwd="/", timeout=0.2)


# --------------------------------------------------------------------------- #
# read_file
# --------------------------------------------------------------------------- #
def test_read_text(share):
    got = sh.read_file(share, "src/m.py")
    assert got == {
        "path": "src/m.py",
        "size": 12,
        "encoding": "utf-8",
        "content": "x = 1\ny = 2\n",
        "truncated": False,
    }
    assert sh.read_file(share, "./src/./m.py")["path"] == "src/m.py"


def test_read_binary_is_base64(share):
    data = bytes(range(256))
    with open(os.path.join(share.work, "bin"), "wb") as f:
        f.write(data)
    got = sh.read_file(share, "bin")
    import base64

    assert got["encoding"] == "base64"
    assert base64.b64decode(got["content"]) == data


def test_read_truncates_and_keeps_utf8_boundary(share):
    with open(os.path.join(share.work, "u.txt"), "w", encoding="utf-8") as f:
        f.write("é" * 100)  # 2 bytes each
    got = sh.read_file(share, "u.txt", max_bytes=11)
    assert got["truncated"] is True
    assert got["encoding"] == "utf-8"
    assert got["content"] == "é" * 5
    assert got["size"] == 200


def test_read_empty_file(share):
    open(os.path.join(share.work, "empty"), "w").close()
    got = sh.read_file(share, "empty")
    assert got["content"] == "" and got["size"] == 0 and not got["truncated"]


def test_read_missing_is_not_found(share):
    with pytest.raises(sh.ShareError, match="^not found$"):
        sh.read_file(share, "nope.txt")


# --------------------------------------------------------------------------- #
# list_files
# --------------------------------------------------------------------------- #
def test_list_files(share):
    open(os.path.join(share.work, "new.txt"), "w").write("n\n")
    open(os.path.join(share.work, "debug.log"), "w").write("ignored\n")
    got = sh.list_files(share)
    assert got["truncated"] is False
    assert sorted(got["files"]) == [".gitignore", "a.txt", "new.txt", "src/m.py"]


def test_list_files_limit(share):
    for i in range(10):
        open(os.path.join(share.work, "f%d" % i), "w").write("x")
    got = sh.list_files(share, limit=5)
    assert len(got["files"]) == 5 and got["truncated"] is True


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def test_diff_clean(share):
    assert sh.diff(share, 10000) == {"stat": [], "diff": "", "truncated": False}


def test_diff_tracked_and_untracked(share):
    with open(os.path.join(share.work, "a.txt"), "a") as f:
        f.write("gamma\n")
    os.unlink(os.path.join(share.work, "src", "m.py"))
    open(os.path.join(share.work, "new.txt"), "w").write("one\ntwo\n")
    open(os.path.join(share.work, "x.log"), "w").write("ignored\n")
    got = sh.diff(share, 100000)
    assert got["truncated"] is False
    rows = {r["path"]: r for r in got["stat"]}
    assert rows["a.txt"]["added"] == 1 and rows["a.txt"]["deleted"] == 0
    assert rows["src/m.py"]["deleted"] == 2
    assert rows["new.txt"] == {
        "path": "new.txt",
        "added": 2,
        "deleted": 0,
        "status": "untracked",
    }
    assert "x.log" not in rows
    assert "+gamma\n" in got["diff"]
    assert "+++ b/new.txt\n@@ -0,0 +1,2 @@\n+one\n+two\n" in got["diff"]
    # the index (the host's) was not touched: new.txt is still untracked
    assert sh.git(share, "ls-files", "new.txt").stdout == b""


def test_diff_whole_hunks_only(share):
    lines = ["line %d\n" % i for i in range(200)]
    p = os.path.join(share.work, "big.txt")
    open(p, "w").writelines(lines)
    sh.checkpoint(share, "big")
    # pretend big.txt was already in the commit the share started from
    sh.git(share, "update-ref", sh.BASE_REF, "HEAD")
    changed = list(lines)
    for i in (5, 100, 190):  # three separate hunks
        changed[i] = "CHANGED %d\n" % i
    open(p, "w").writelines(changed)
    full = sh.diff(share, 200000)
    assert full["diff"].count("@@ -") == 3
    budget = full["diff"].index("@@", full["diff"].index("CHANGED 100"))
    part = sh.diff(share, budget)
    assert part["truncated"] is True
    assert part["diff"].count("@@ -") == 2
    assert "CHANGED 100" in part["diff"] and "CHANGED 190" not in part["diff"]
    assert full["diff"].startswith(part["diff"])
    tiny = sh.diff(share, 10)
    assert tiny["diff"] == "" and tiny["truncated"] is True
    assert tiny["stat"]  # the stat is always complete


def test_diff_untracked_binary(share):
    open(os.path.join(share.work, "b.bin"), "wb").write(b"\x00\x01\x02")
    got = sh.diff(share, 10000)
    assert "Binary files /dev/null and b/b.bin differ" in got["diff"]


# --------------------------------------------------------------------------- #
# checkpoint
# --------------------------------------------------------------------------- #
def test_checkpoint_commits_everything(share):
    before = sh.git(share, "rev-parse", "HEAD").stdout.decode().strip()
    open(os.path.join(share.work, "new.txt"), "w").write("n\n")
    os.unlink(os.path.join(share.work, "a.txt"))
    sha = sh.checkpoint(share, "my work")
    assert sha != before and len(sha) == 40
    assert sh.git(share, "rev-parse", "HEAD").stdout.decode().strip() == sha
    files = sh.git(share, "ls-tree", "-r", "--name-only", "HEAD").stdout.decode()
    assert "new.txt" in files and "a.txt" not in files
    who = sh.git(share, "log", "-1", "--format=%an|%ae|%cn|%ce|%B").stdout.decode()
    assert who.startswith(
        "MindFlock peer|peer@mindflock.invalid|MindFlock peer|peer@mindflock.invalid|"
    )
    assert "my work" in who
    # the peer's diff is vs the share's start, so checkpointed work stays visible
    assert {r["path"] for r in sh.diff(share, 1000)["stat"]} == {"new.txt", "a.txt"}


def test_checkpoint_nothing_to_commit_returns_head(share):
    head = sh.git(share, "rev-parse", "HEAD").stdout.decode().strip()
    assert sh.checkpoint(share, "noop") == head


@pytest.mark.parametrize(
    "raw,want",
    [
        ("", "peer checkpoint"),
        (None, "peer checkpoint"),
        ("   ", "peer checkpoint"),
        ("ok\x1b[2J\x07done", "ok[2Jdone"),
        ("a\r\nb\rc", "a\nb\nc"),
        ("x" * 900, "x" * 500),
        ("rtl‮evil", "rtlevil"),
        ("tab\there", "tab\there"),
    ],
)
def test_sanitize_message(raw, want):
    assert sh.sanitize_message(raw) == want


def test_checkpoint_message_is_sanitized(share):
    open(os.path.join(share.work, "n"), "w").write("n")
    sh.checkpoint(share, "--amend\n\x1b]0;pwn\x07" + "y" * 1000)
    body = sh.git(share, "log", "-1", "--format=%B").stdout.decode()
    assert "\x1b" not in body and "\x07" not in body
    assert body.startswith("--amend\n]0;pwn")
    assert len(body.strip()) <= 500
    assert sh.git(share, "rev-list", "--count", "HEAD").stdout.strip() == b"2"


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def test_export_into_trusted_repo(share, repo):
    open(os.path.join(share.work, "peer.txt"), "w").write("from peer\n")
    head_before = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    out = sh.export(share, str(repo), "peer/collab")
    assert out["branch"] == "peer/collab"
    assert out["target"] == str(repo.resolve())
    assert run_git(repo, "rev-parse", "peer/collab").stdout.strip() == out["sha"]
    assert run_git(repo, "show", "peer/collab:peer.txt").stdout == "from peer\n"
    # nothing checked out or changed in the target's work tree
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert not (repo / "peer.txt").exists()
    assert run_git(repo, "status", "--porcelain").stdout == ""
    # a second export force-updates the same peer/ branch
    open(os.path.join(share.work, "peer.txt"), "w").write("v2\n")
    out2 = sh.export(share, str(repo), "peer/collab")
    assert out2["sha"] != out["sha"]
    assert run_git(repo, "show", "peer/collab:peer.txt").stdout == "v2\n"


def test_export_refuses_checked_out_branch(share, repo):
    run_git(repo, "checkout", "-q", "-b", "peer/live")
    with pytest.raises(sh.ShareError, match="checked out"):
        sh.export(share, str(repo), "peer/live")
    wt = repo.parent / "wt"
    run_git(repo, "checkout", "-q", "main")
    run_git(repo, "worktree", "add", "-q", "-b", "peer/inwt", str(wt))
    with pytest.raises(sh.ShareError, match="checked out"):
        sh.export(share, str(repo), "peer/inwt")


def test_export_refuses_non_repo(share, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(sh.ShareError):
        sh.export(share, str(plain), "peer/x")
    with pytest.raises(sh.ShareError):
        sh.export(share, str(tmp_path / "missing"), "peer/x")


# --------------------------------------------------------------------------- #
# remove_share
# --------------------------------------------------------------------------- #
def test_remove_share(share):
    assert sh.remove_share(share.share_id) is True
    assert not os.path.exists(share.root)
    assert sh.remove_share(share.share_id) is False


def test_remove_share_refuses_running(share):
    calls = []

    def running(sid):
        calls.append(sid)
        return True

    with pytest.raises(sh.ShareError, match="running"):
        sh.remove_share(share.share_id, is_running=running)
    assert calls == [share.share_id]
    assert os.path.isdir(share.root)
    assert sh.remove_share(share.share_id, is_running=lambda sid: False)
    assert not os.path.exists(share.root)


def test_remove_share_handles_locked_down_dirs(share):
    d = os.path.join(share.work, "locked", "deeper")
    os.makedirs(d)
    open(os.path.join(d, "f"), "w").write("x")
    os.chmod(d, 0o000)
    os.chmod(os.path.dirname(d), 0o500)
    assert sh.remove_share(share.share_id)
    assert not os.path.exists(share.root)


def test_diff_huge_tracked_change_truncates_not_fails(share):
    """git output past the read cap is cut (git is killed) — that must read
    as truncation, never as a failed git call."""
    open(os.path.join(share.work, "a.txt"), "w").write(
        "".join("changed line %d %s\n" % (i, "x" * 60) for i in range(20000))
    )
    got = sh.diff(share, 1000)
    assert got["truncated"] is True
    assert len(got["diff"]) <= 1000
    assert got["stat"][0]["path"] == "a.txt"
    res = sh.git(share, "diff", "HEAD", max_output=500)  # check=True
    assert res.truncated and len(res.stdout) == 500


def test_list_files_output_cap_is_truncation(share, monkeypatch):
    for i in range(50):
        open(os.path.join(share.work, "file-%03d.txt" % i), "w").write("x")
    real = sh.git

    def capped(s, *a, **kw):
        kw["max_output"] = 200
        return real(s, *a, **kw)

    monkeypatch.setattr(sh, "git", capped)
    got = sh.list_files(share)
    assert got["truncated"] is True and 0 < len(got["files"]) < 54


def test_diff_includes_checkpointed_changes(share):
    """A checkpoint commits the work; the peer's diff must still show it
    (it compares against the share's starting commit, not HEAD)."""
    with open(os.path.join(share.work, "a.txt"), "a") as f:
        f.write("checkpointed line\n")
    sh.checkpoint(share, "work")
    with open(os.path.join(share.work, "new.txt"), "w") as f:
        f.write("loose\n")
    out = sh.diff(share, 100000)
    assert "checkpointed line" in out["diff"]
    assert "loose" in out["diff"]
    paths_ = {r["path"] for r in out["stat"]}
    assert {"a.txt", "new.txt"} <= paths_
