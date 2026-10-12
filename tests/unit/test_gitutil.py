"""backend.gitutil — the shared git runner and its worktree/ref helpers."""

from __future__ import annotations

import subprocess
import time

import pytest

from backend import gitutil


def _sh(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _sh(r, "config", "user.email", "t@t.local")
    _sh(r, "config", "user.name", "T")
    (r / "a.txt").write_text("a\n")
    _sh(r, "add", "a.txt")
    _sh(r, "commit", "-q", "-m", "first")
    return r


# --------------------------------------------------------------------------- #
# git()
# --------------------------------------------------------------------------- #
def test_git_runs_with_dash_c_and_decodes(repo):
    cp = gitutil.git(["rev-parse", "--abbrev-ref", "HEAD"], str(repo), timeout=30)
    assert cp.args[:3] == ["git", "-C", str(repo)]
    assert cp.returncode == 0 and cp.stdout == "main\n"
    assert not cp.timed_out and not cp.not_run


def test_git_bytes_mode_and_devnull_stderr(repo):
    cp = gitutil.git(
        ["cat-file", "-t", "nope"],
        str(repo),
        timeout=30,
        text=False,
        stderr=subprocess.DEVNULL,
    )
    assert cp.returncode != 0
    assert cp.stdout == b"" and cp.stderr == b""


def test_git_without_cwd_has_no_dash_c():
    cp = gitutil.git(["--version"], timeout=30)
    assert cp.args == ["git", "--version"] and cp.stdout.startswith("git version")


def test_git_env_overlays_os_environ(repo, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "kept")
    cp = gitutil.git(
        ["var", "GIT_AUTHOR_IDENT"], str(repo), timeout=30, env={"LC_ALL": "C"}
    )
    assert cp.stdout.startswith("kept ")


def test_git_input_feeds_stdin(repo):
    cp = gitutil.git(
        ["hash-object", "--stdin"], str(repo), timeout=30, input=b"hello\n"
    )
    want = subprocess.run(
        ["git", "hash-object", "--stdin"], input=b"hello\n", capture_output=True
    ).stdout.decode()
    assert cp.returncode == 0 and cp.stdout == want


def test_git_non_utf8_output_never_raises(repo, monkeypatch):
    real = subprocess.run

    def fake(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, b"caf\xe9\n", b"")

    monkeypatch.setattr(subprocess, "run", fake)
    assert gitutil.git(["log"], str(repo), timeout=30).stdout == "caf�\n"
    monkeypatch.setattr(subprocess, "run", real)


def test_git_timeout_is_a_failed_result(monkeypatch):
    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"], output=b"part")

    monkeypatch.setattr(subprocess, "run", hang)
    cp = gitutil.git(["fetch"], "/r", timeout=7)
    assert cp.returncode == gitutil.TIMEOUT_RC and cp.timed_out
    assert cp.stdout == "part" and "timed out after 7s" in cp.stderr
    with pytest.raises(subprocess.CalledProcessError):
        gitutil.git(["fetch"], "/r", timeout=7, check=True)


def test_git_missing_binary_is_a_failed_result(monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError(2, "No such file", "git")

    monkeypatch.setattr(subprocess, "run", missing)
    cp = gitutil.git(["status"], "/r", timeout=5)
    assert cp.returncode == gitutil.LAUNCH_RC and cp.not_run
    assert cp.stdout == "" and "No such file" in cp.stderr


def test_git_nul_byte_argument_is_not_run():
    cp = gitutil.git(["status"], "/tmp/a\x00b", timeout=5)
    assert cp.not_run and cp.returncode == gitutil.LAUNCH_RC


def test_git_check_raises_on_nonzero(repo):
    with pytest.raises(subprocess.CalledProcessError):
        gitutil.git(
            ["rev-parse", "--verify", "nope"], str(repo), timeout=30, check=True
        )


def test_new_session_timeout_kills_the_whole_group(repo):
    """A shell alias's child holds the pipes open: killing git alone would
    leave communicate() waiting for the sleep."""
    t0 = time.monotonic()
    cp = gitutil.git(
        ["-c", "alias.slp=!sleep 20", "slp"],
        str(repo),
        timeout=0.5,
        new_session=True,
    )
    assert cp.timed_out and cp.returncode == gitutil.TIMEOUT_RC
    assert time.monotonic() - t0 < 10


def test_new_session_success(repo):
    cp = gitutil.git(["rev-parse", "HEAD"], str(repo), timeout=30, new_session=True)
    assert cp.returncode == 0 and len(cp.stdout.strip()) == 40


# --------------------------------------------------------------------------- #
# git_out()
# --------------------------------------------------------------------------- #
def test_git_out_strips_and_blanks_failures(repo, tmp_path):
    assert gitutil.git_out(["branch", "--show-current"], str(repo), timeout=30) == (
        "main"
    )
    assert gitutil.git_out(["rev-parse", "nope"], str(repo), timeout=30) == ""
    assert gitutil.git_out(["status"], str(tmp_path / "gone"), timeout=30) == ""


# --------------------------------------------------------------------------- #
# worktree list
# --------------------------------------------------------------------------- #
_PORCELAIN = """\
worktree /repo
HEAD 1111111111111111111111111111111111111111
branch refs/heads/main

worktree /wt/feature x
HEAD 2222222222222222222222222222222222222222
branch refs/heads/feature/x
locked reason here

worktree /wt/detached
HEAD 3333333333333333333333333333333333333333
detached
prunable gitdir file points to non-existent location

worktree /bare
bare
"""


def test_parse_worktree_list_records():
    wts = gitutil.parse_worktree_list(_PORCELAIN)
    assert [w["path"] for w in wts] == [
        "/repo",
        "/wt/feature x",
        "/wt/detached",
        "/bare",
    ]
    assert wts[0]["branch"] == "refs/heads/main" and wts[0]["head"].startswith("1")
    assert wts[1]["locked"] == "reason here"
    assert wts[2]["detached"] is True and "branch" not in wts[2]
    assert wts[2]["prunable"].startswith("gitdir")
    assert wts[3] == {"path": "/bare", "bare": True}


def test_parse_worktree_list_tolerates_junk():
    assert gitutil.parse_worktree_list("") == []
    assert gitutil.parse_worktree_list("branch refs/heads/x\n") == []
    # no blank separator between records
    wts = gitutil.parse_worktree_list("worktree /a\nworktree /b\nbranch refs/heads/b\n")
    assert wts == [{"path": "/a"}, {"path": "/b", "branch": "refs/heads/b"}]


def test_worktree_list_and_branch_holder(repo, tmp_path):
    wt = tmp_path / "wt"
    _sh(repo, "worktree", "add", "-q", "-b", "feature/x", str(wt))
    wts = gitutil.worktree_list(str(repo))
    assert [w.get("branch") for w in wts] == ["refs/heads/main", "refs/heads/feature/x"]
    assert gitutil.worktree_for_branch(str(repo), "feature/x") == str(wt)
    assert gitutil.worktree_for_branch(str(repo), "nope") == ""


def test_worktree_list_failure_is_none(tmp_path):
    assert gitutil.worktree_list(str(tmp_path)) is None
    assert gitutil.worktree_for_branch(str(tmp_path), "main") == ""


# --------------------------------------------------------------------------- #
# ref_exists()
# --------------------------------------------------------------------------- #
def test_ref_exists(repo, tmp_path):
    assert gitutil.ref_exists(str(repo), "refs/heads/main")
    assert gitutil.ref_exists(str(repo), "HEAD")
    assert not gitutil.ref_exists(str(repo), "refs/heads/ghost")
    assert not gitutil.ref_exists(str(repo), "--all")
    assert not gitutil.ref_exists(str(repo), "")
    assert not gitutil.ref_exists(str(tmp_path), "HEAD")
