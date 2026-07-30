"""Hermetic tests for :mod:`backend.session.git.worktree_git`.

Covers the ``GitWorktreeGitMixin`` methods (IsDirty / IsValidWorktree /
IsBranchCheckedOut / CommitChanges / PushChanges / OpenBranchURL) plus the
``fetch_branches`` best-effort helper and ``search_branches``'s timeout path.

Every git/gh invocation is mocked at the :mod:`subprocess` seam, so no network,
no gh auth, and no real pushes happen. gh is treated as **absent by default**:
these tests must pass on a machine that has never installed the GitHub CLI, and
the tests that care about gh stub ``gh_available`` explicitly. The Go-parity
error strings are pinned exactly.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from backend.session.git import worktree_git
from backend.session.git.worktree import GitWorktree
from backend.session.git.worktree_git import fetch_branches, search_branches


# ---------------------------------------------------------------------------
# Fake subprocess result + a dispatcher keyed on argv
# ---------------------------------------------------------------------------
class _Completed:
    def __init__(self, returncode: int = 0, stdout: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout


def _wt(tmp_path, branch="feature-x"):
    """A GitWorktree whose paths point at tmp dirs (no real git needed)."""
    return GitWorktree(
        repoPath=str(tmp_path / "repo"),
        worktreePath=str(tmp_path / "wt"),
        sessionName="sess",
        branchName=branch,
    )


# ---------------------------------------------------------------------------
# fetch_branches — best-effort: never raises
# ---------------------------------------------------------------------------
def test_fetch_branches_ignores_failure(monkeypatch):
    def _fail(*a, **k):
        return _Completed(returncode=1)

    monkeypatch.setattr(worktree_git.subprocess, "run", _fail)
    # Must not raise even though the underlying fetch "failed".
    assert fetch_branches("/repo") is None


def test_fetch_branches_swallows_timeout(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git fetch", timeout=600)

    monkeypatch.setattr(worktree_git.subprocess, "run", _boom)
    assert fetch_branches("/repo") is None


def test_fetch_branches_argv_is_fetch_prune(monkeypatch):
    seen = {}

    def _capture(argv, *a, **k):
        seen["argv"] = argv
        return _Completed()

    monkeypatch.setattr(worktree_git.subprocess, "run", _capture)
    fetch_branches("/repo/x")
    assert seen["argv"] == ["git", "-C", "/repo/x", "fetch", "--prune"]


# ---------------------------------------------------------------------------
# search_branches — timeout branch (happy paths already in test_worktree_ops)
# ---------------------------------------------------------------------------
def test_search_branches_timeout_raises(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git branch", timeout=60, output=b"part")

    monkeypatch.setattr(worktree_git.subprocess, "run", _boom)
    with pytest.raises(RuntimeError) as ei:
        search_branches("/repo", "")
    msg = str(ei.value)
    assert "failed to list branches" in msg
    assert "timed out after 60s" in msg
    assert "part" in msg


# ---------------------------------------------------------------------------
# IsDirty — non-empty porcelain == dirty
# ---------------------------------------------------------------------------
def test_is_dirty_true_when_status_nonempty(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        lambda *a, **k: _Completed(stdout=b" M file.py\n"),
    )
    assert wt.IsDirty() is True


def test_is_dirty_false_when_status_empty(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess, "run", lambda *a, **k: _Completed(stdout=b"")
    )
    assert wt.IsDirty() is False


def test_is_dirty_wraps_failure(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        lambda *a, **k: _Completed(returncode=128, stdout=b"fatal: bad"),
    )
    with pytest.raises(RuntimeError) as ei:
        wt.IsDirty()
    assert "failed to check worktree status" in str(ei.value)


# ---------------------------------------------------------------------------
# IsValidWorktree — path + .git existence
# ---------------------------------------------------------------------------
def test_is_valid_worktree_true_when_path_and_git_present(tmp_path):
    wtp = tmp_path / "wt"
    wtp.mkdir()
    (wtp / ".git").write_text("gitdir: ...\n")
    wt = GitWorktree(worktreePath=str(wtp), branchName="b")
    assert wt.IsValidWorktree() is True


def test_is_valid_worktree_false_when_path_missing(tmp_path):
    wt = GitWorktree(worktreePath=str(tmp_path / "gone"), branchName="b")
    assert wt.IsValidWorktree() is False


def test_is_valid_worktree_false_when_git_missing(tmp_path):
    wtp = tmp_path / "wt"
    wtp.mkdir()  # exists, but no .git inside
    wt = GitWorktree(worktreePath=str(wtp), branchName="b")
    assert wt.IsValidWorktree() is False


# ---------------------------------------------------------------------------
# IsBranchCheckedOut — compares `branch --show-current` to branchName
# ---------------------------------------------------------------------------
def test_is_branch_checked_out_true(monkeypatch, tmp_path):
    wt = _wt(tmp_path, branch="feature-x")
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        lambda *a, **k: _Completed(stdout=b"feature-x\n"),
    )
    assert wt.IsBranchCheckedOut() is True


def test_is_branch_checked_out_false(monkeypatch, tmp_path):
    wt = _wt(tmp_path, branch="feature-x")
    monkeypatch.setattr(
        worktree_git.subprocess, "run", lambda *a, **k: _Completed(stdout=b"main\n")
    )
    assert wt.IsBranchCheckedOut() is False


def test_is_branch_checked_out_wraps_failure(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        lambda *a, **k: _Completed(returncode=1, stdout=b"err"),
    )
    with pytest.raises(RuntimeError) as ei:
        wt.IsBranchCheckedOut()
    assert "failed to get current branch" in str(ei.value)


# ---------------------------------------------------------------------------
# CommitChanges — stages + commits only when dirty
# ---------------------------------------------------------------------------
def test_commit_changes_stages_and_commits_when_dirty(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    calls = []

    def _run(argv, *a, **k):
        calls.append(tuple(argv[3:]))  # drop ["git","-C",path]
        # status --porcelain -> dirty
        if argv[3:] == ["status", "--porcelain"]:
            return _Completed(stdout=b" M x\n")
        return _Completed(stdout=b"")

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    wt.CommitChanges("my message")

    assert ("add", ".") in calls
    # commit runs with --no-verify and the exact message.
    assert ("commit", "-m", "my message", "--no-verify") in calls


def test_commit_changes_noop_when_clean(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    calls = []

    def _run(argv, *a, **k):
        calls.append(tuple(argv[3:]))
        return _Completed(stdout=b"")  # status empty -> clean

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    wt.CommitChanges("m")
    # Only the status check ran; no add/commit.
    assert calls == [("status", "--porcelain")]


def test_commit_changes_wraps_dirty_check_failure(monkeypatch, tmp_path):
    wt = _wt(tmp_path)

    def _run(argv, *a, **k):
        return _Completed(returncode=1, stdout=b"boom")

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    with pytest.raises(RuntimeError) as ei:
        wt.CommitChanges("m")
    assert "failed to check for changes" in str(ei.value)


# ---------------------------------------------------------------------------
# OpenBranchURL — gh browse when gh is usable, plain branch URL otherwise
# ---------------------------------------------------------------------------
def _capture_browser(monkeypatch):
    """Record URLs handed to webbrowser.open (which reports success)."""
    opened = []
    monkeypatch.setattr(
        worktree_git.webbrowser, "open", lambda url: opened.append(url) or True
    )
    return opened


def _origin_run(origin, gh=None):
    """subprocess.run stub answering `git -C <wt> remote get-url origin`.

    ``gh`` is what a ``gh browse`` invocation should do: ``None`` means gh must
    never be invoked (the stub fails the test if it is), otherwise a callable.
    """

    def _run(argv, *a, **k):
        if argv[0] == "gh":
            if gh is None:
                raise AssertionError("gh must not run: {}".format(argv))
            return gh(argv)
        if argv[3:] == ["remote", "get-url", "origin"]:
            return _Completed(stdout=origin.encode() + b"\n")
        return _Completed(stdout=b"")

    return _run


def test_open_branch_url_uses_gh_browse_when_available(monkeypatch, tmp_path):
    wt = _wt(tmp_path, branch="feature-x")
    monkeypatch.setattr(worktree_git, "gh_available", lambda: True)
    opened = _capture_browser(monkeypatch)
    seen = {}

    def _capture(argv, *a, **k):
        seen["argv"] = argv
        seen["cwd"] = k.get("cwd")
        return _Completed(returncode=0)

    monkeypatch.setattr(worktree_git.subprocess, "run", _capture)
    assert wt.OpenBranchURL() is None
    assert seen["argv"] == ["gh", "browse", "--branch", "feature-x"]
    # gh runs with cwd == the worktree path (no -C), like Go.
    assert seen["cwd"] == wt.worktreePath
    # gh already opened the page; we must not open a second tab.
    assert opened == []


def test_open_branch_url_ssh_origin_without_gh(monkeypatch, tmp_path):
    """The reported bug: an SSH remote and no gh still opens the branch page."""
    wt = _wt(tmp_path, branch="feature-x")
    monkeypatch.setattr(worktree_git, "gh_available", lambda: False)
    opened = _capture_browser(monkeypatch)
    monkeypatch.setattr(
        worktree_git.subprocess, "run", _origin_run("git@github.com:Org/app.git")
    )
    assert wt.OpenBranchURL() is None
    assert opened == ["https://github.com/Org/app/tree/feature-x"]


def test_open_branch_url_https_origin_without_gh(monkeypatch, tmp_path):
    wt = _wt(tmp_path, branch="feature-x")
    monkeypatch.setattr(worktree_git, "gh_available", lambda: False)
    opened = _capture_browser(monkeypatch)
    monkeypatch.setattr(
        worktree_git.subprocess, "run", _origin_run("https://github.com/Org/app.git")
    )
    assert wt.OpenBranchURL() is None
    # Both spellings resolve to the same page.
    assert opened == ["https://github.com/Org/app/tree/feature-x"]


def test_open_branch_url_gh_timeout_falls_through_to_url(monkeypatch, tmp_path):
    """A hung gh must not surface as an error — the URL path reaches the page."""
    wt = _wt(tmp_path, branch="feature-x")
    monkeypatch.setattr(worktree_git, "gh_available", lambda: True)
    opened = _capture_browser(monkeypatch)

    def _hang(_argv):
        raise subprocess.TimeoutExpired(cmd="gh browse", timeout=60)

    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        _origin_run("git@github.com:Org/app.git", gh=_hang),
    )
    assert wt.OpenBranchURL() is None
    assert opened == ["https://github.com/Org/app/tree/feature-x"]


def test_open_branch_url_gh_failure_falls_through_to_url(monkeypatch, tmp_path):
    wt = _wt(tmp_path, branch="feature-x")
    monkeypatch.setattr(worktree_git, "gh_available", lambda: True)
    opened = _capture_browser(monkeypatch)
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        _origin_run(
            "https://github.com/Org/app", gh=lambda _argv: _Completed(returncode=1)
        ),
    )
    assert wt.OpenBranchURL() is None
    assert opened == ["https://github.com/Org/app/tree/feature-x"]


def test_open_branch_url_local_origin_raises(monkeypatch, tmp_path):
    """A local clone source has no branch page; say so instead of guessing."""
    wt = _wt(tmp_path)
    monkeypatch.setattr(worktree_git, "gh_available", lambda: False)
    _capture_browser(monkeypatch)
    monkeypatch.setattr(worktree_git.subprocess, "run", _origin_run("/home/me/app"))
    with pytest.raises(RuntimeError) as ei:
        wt.OpenBranchURL()
    assert "failed to open branch URL" in str(ei.value)
    assert "local path" in str(ei.value)
    assert "/home/me/app" in str(ei.value)


def test_open_branch_url_unrecognised_origin_raises(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(worktree_git, "gh_available", lambda: False)
    _capture_browser(monkeypatch)
    monkeypatch.setattr(worktree_git.subprocess, "run", _origin_run("not-a-url"))
    with pytest.raises(RuntimeError) as ei:
        wt.OpenBranchURL()
    assert "not a recognised forge URL" in str(ei.value)


def test_open_branch_url_remote_lookup_failure_raises(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(worktree_git, "gh_available", lambda: False)
    _capture_browser(monkeypatch)
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        lambda *a, **k: _Completed(returncode=2, stdout=b"no such remote"),
    )
    with pytest.raises(RuntimeError) as ei:
        wt.OpenBranchURL()
    assert "could not read remote 'origin'" in str(ei.value)


def test_open_branch_url_no_browser_raises(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(worktree_git, "gh_available", lambda: False)
    monkeypatch.setattr(worktree_git.webbrowser, "open", lambda _url: False)
    monkeypatch.setattr(
        worktree_git.subprocess, "run", _origin_run("git@github.com:Org/app.git")
    )
    with pytest.raises(RuntimeError) as ei:
        wt.OpenBranchURL()
    assert "no browser available" in str(ei.value)


# ---------------------------------------------------------------------------
# PushChanges — commit, then plain `git push` (no gh, ever)
# ---------------------------------------------------------------------------
def _push_dispatcher(states):
    """Build a subprocess.run stub dispatching on argv[0]+subcommand.

    ``states`` maps a key to a returncode; keys:
      * "dirty"       truthy -> git status --porcelain reports changes
      * "add"/"commit" git staging/commit
      * "git_push"    the bare `git push -u origin <branch>`

    There is no gh key: pushing is plain git end to end, so any ``gh``
    invocation is a bug and fails the test outright.
    """

    def _run(argv, *a, **k):
        prog = argv[0]
        if prog == "git":
            # The bare `git push -u origin <branch>` has no `-C`, so its
            # subcommand is argv[1]; the -C-prefixed commands put it at argv[3].
            if "push" in argv:
                return _Completed(
                    returncode=states.get("git_push", 0), stdout=b"push-out"
                )
            sub = argv[3] if len(argv) > 3 else ""
            if sub == "status":
                return _Completed(
                    returncode=0,
                    stdout=b" M x\n" if states.get("dirty") else b"",
                )
            if sub == "add":
                return _Completed(returncode=states.get("add", 0))
            if sub == "commit":
                return _Completed(returncode=states.get("commit", 0))
            return _Completed(returncode=0)
        raise AssertionError("PushChanges must only run git, got: {}".format(argv))

    return _run


def test_push_changes_clean_repo_pushes_ok(monkeypatch, tmp_path):
    wt = _wt(tmp_path, branch="feature-x")
    calls = []
    disp = _push_dispatcher({"dirty": False})

    def _run(argv, *a, **k):
        calls.append((argv, k.get("cwd")))
        return disp(argv, *a, **k)

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    # open=False so OpenBranchURL is not invoked.
    assert wt.PushChanges("msg", False) is None
    # The canonical success path is a bare `git push` (no -C) run in the
    # worktree — previously this test passed without ever pushing at all.
    assert (["git", "push", "-u", "origin", "feature-x"], wt.worktreePath) in calls


def test_push_changes_issues_no_gh_command(monkeypatch, tmp_path):
    """gh is not installed here, and the push must not care."""
    wt = _wt(tmp_path)

    def _gh_must_not_be_consulted():
        raise AssertionError("PushChanges must not depend on gh at all")

    monkeypatch.setattr(worktree_git, "gh_available", _gh_must_not_be_consulted)
    calls = []
    disp = _push_dispatcher({"dirty": True, "add": 0, "commit": 0})

    def _run(argv, *a, **k):
        calls.append(argv)
        return disp(argv, *a, **k)

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    assert wt.PushChanges("msg", False) is None
    assert not any(a[0] == "gh" for a in calls)


def test_push_changes_push_failure_raises(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        _push_dispatcher({"dirty": False, "git_push": 1}),
    )
    with pytest.raises(RuntimeError) as ei:
        wt.PushChanges("msg", False)
    assert "failed to push branch" in str(ei.value)
    assert "exit status 1" in str(ei.value)


def test_push_changes_succeeds_when_gh_is_broken(monkeypatch, tmp_path):
    """A failing gh must not fail a push that already succeeded.

    The old flow ended with `gh repo sync`, whose non-zero exit raised
    "failed to sync changes" — so a perfectly successful push was reported as a
    failure to everyone without a working gh.
    """
    wt = _wt(tmp_path)

    def _run(argv, *a, **k):
        if argv[0] == "gh":
            return _Completed(returncode=1, stdout=b"gh: command not usable")
        if "push" in argv:
            return _Completed(returncode=0, stdout=b"")
        return _Completed(returncode=0, stdout=b"")  # status -> clean

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    assert wt.PushChanges("msg", False) is None


def test_push_changes_succeeds_when_gh_hangs(monkeypatch, tmp_path):
    """Same for a hung gh: the push already landed, so it is not an error."""
    wt = _wt(tmp_path)

    def _run(argv, *a, **k):
        if argv[0] == "gh":
            raise subprocess.TimeoutExpired(cmd="gh sync", timeout=600, output=b"so")
        return _Completed(returncode=0, stdout=b"")  # status clean, then push

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    assert wt.PushChanges("msg", False) is None


def test_push_changes_dirty_commits_before_pushing(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    subs = []
    disp = _push_dispatcher({"dirty": True, "add": 0, "commit": 0})

    def _run(argv, *a, **k):
        subs.append((argv[0], argv[3] if argv[0] == "git" and len(argv) > 3 else ""))
        return disp(argv, *a, **k)

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    assert wt.PushChanges("commit-me", False) is None
    # Dirty path stages then commits before the push.
    assert ("git", "add") in subs
    assert ("git", "commit") in subs


def test_push_changes_opens_branch_url_when_open_true(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess, "run", _push_dispatcher({"dirty": False})
    )
    opened = {"n": 0}
    monkeypatch.setattr(
        wt, "OpenBranchURL", lambda: opened.__setitem__("n", opened["n"] + 1)
    )
    wt.PushChanges("msg", True)
    assert opened["n"] == 1


def test_push_changes_open_failure_is_swallowed(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess, "run", _push_dispatcher({"dirty": False})
    )

    def _raise():
        raise RuntimeError("browser missing")

    monkeypatch.setattr(wt, "OpenBranchURL", _raise)
    # open=True but OpenBranchURL failing must NOT fail the push.
    assert wt.PushChanges("msg", True) is None


# ---------------------------------------------------------------------------
# run_git_command — combined-output error format + timeout
# ---------------------------------------------------------------------------
def test_run_git_command_success_returns_decoded_output(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        lambda *a, **k: _Completed(stdout=b"hello\n"),
    )
    assert wt.run_git_command(str(tmp_path), "status") == "hello\n"


def test_run_git_command_failure_uses_go_format(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        lambda *a, **k: _Completed(returncode=2, stdout=b"nope"),
    )
    with pytest.raises(RuntimeError) as ei:
        wt.run_git_command(str(tmp_path), "bad")
    assert str(ei.value) == "git command failed: nope (exit status 2)"


def test_run_git_command_timeout_uses_go_format(monkeypatch, tmp_path):
    wt = _wt(tmp_path)

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=60, output=b"stuck")

    monkeypatch.setattr(worktree_git.subprocess, "run", _boom)
    with pytest.raises(RuntimeError) as ei:
        wt.run_git_command(str(tmp_path), "bad")
    assert "git command failed: stuck (timed out after 60s)" == str(ei.value)


# ---------------------------------------------------------------------------
# A capturing logger so the ErrorLog branches execute (and can be asserted).
# ---------------------------------------------------------------------------
class _CapLog:
    def __init__(self):
        self.msgs = []

    def Print(self, *args):
        self.msgs.append(" ".join(str(a) for a in args))

    def Printf(self, fmt, *args):
        # Loggers use Go-style verbs (%v) that Python's % rejects; just append
        # the args so message-substring assertions still work.
        self.msgs.append(fmt + " " + " ".join(str(a) for a in args))

    def Println(self, *args):
        self.msgs.append(" ".join(str(a) for a in args))


@pytest.fixture
def caplog_errorlog(monkeypatch):
    cap = _CapLog()
    monkeypatch.setattr(worktree_git.log, "ErrorLog", cap)
    return cap


# ---------------------------------------------------------------------------
# PushChanges — dirty-check / stage / commit failures (with logging)
# ---------------------------------------------------------------------------
def test_push_changes_dirty_check_failure_raises(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    # No gh stub anywhere below: these paths never touch gh, and the missing
    # stub is itself the regression test for the removed check_gh_cli() gate.
    # status --porcelain fails -> IsDirty raises -> "failed to check for changes".
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        lambda *a, **k: _Completed(returncode=1, stdout=b"boom"),
    )
    with pytest.raises(RuntimeError) as ei:
        wt.PushChanges("m", False)
    assert "failed to check for changes" in str(ei.value)


def test_push_changes_stage_failure_raises_and_logs(
    monkeypatch, tmp_path, caplog_errorlog
):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        _push_dispatcher({"dirty": True, "add": 1}),
    )
    with pytest.raises(RuntimeError) as ei:
        wt.PushChanges("m", False)
    assert "failed to stage changes" in str(ei.value)
    assert caplog_errorlog.msgs  # the error was logged


def test_push_changes_commit_failure_raises(monkeypatch, tmp_path):
    wt = _wt(tmp_path)
    monkeypatch.setattr(
        worktree_git.subprocess,
        "run",
        _push_dispatcher({"dirty": True, "add": 0, "commit": 1}),
    )
    with pytest.raises(RuntimeError) as ei:
        wt.PushChanges("m", False)
    assert "failed to commit changes" in str(ei.value)


def test_push_changes_git_push_timeout_raises(monkeypatch, tmp_path):
    wt = _wt(tmp_path)

    def _run(argv, *a, **k):
        if "push" in argv:
            raise subprocess.TimeoutExpired(cmd="git push", timeout=600, output=b"po")
        return _Completed(returncode=0, stdout=b"")  # status clean

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    with pytest.raises(RuntimeError) as ei:
        wt.PushChanges("m", False)
    assert "failed to push branch" in str(ei.value)
    assert "timed out after 600s" in str(ei.value)
    assert "po" in str(ei.value)


# ---------------------------------------------------------------------------
# CommitChanges — stage / commit failure branches (with logging)
# ---------------------------------------------------------------------------
def test_commit_changes_stage_failure_raises(monkeypatch, tmp_path, caplog_errorlog):
    wt = _wt(tmp_path)

    def _run(argv, *a, **k):
        if argv[3:] == ["status", "--porcelain"]:
            return _Completed(stdout=b" M x\n")  # dirty
        if argv[3:4] == ["add"]:
            return _Completed(returncode=1, stdout=b"add-fail")
        return _Completed(stdout=b"")

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    with pytest.raises(RuntimeError) as ei:
        wt.CommitChanges("m")
    assert "failed to stage changes" in str(ei.value)
    assert caplog_errorlog.msgs


def test_commit_changes_commit_failure_raises(monkeypatch, tmp_path):
    wt = _wt(tmp_path)

    def _run(argv, *a, **k):
        if argv[3:] == ["status", "--porcelain"]:
            return _Completed(stdout=b" M x\n")  # dirty
        if argv[3:4] == ["add"]:
            return _Completed(returncode=0)
        if argv[3:4] == ["commit"]:
            return _Completed(returncode=1, stdout=b"commit-fail")
        return _Completed(stdout=b"")

    monkeypatch.setattr(worktree_git.subprocess, "run", _run)
    with pytest.raises(RuntimeError) as ei:
        wt.CommitChanges("m")
    assert "failed to commit changes" in str(ei.value)


# ---------------------------------------------------------------------------
# IsValidWorktree — OSError (non-FileNotFound) branches raise
# ---------------------------------------------------------------------------
def test_is_valid_worktree_path_stat_oserror_raises(monkeypatch, tmp_path):
    wt = _wt(tmp_path)

    def _stat(_p):
        raise PermissionError("denied")

    monkeypatch.setattr(worktree_git.os, "stat", _stat)
    with pytest.raises(RuntimeError) as ei:
        wt.IsValidWorktree()
    assert "failed to stat worktree path" in str(ei.value)


def test_is_valid_worktree_git_stat_oserror_raises(monkeypatch, tmp_path):
    wtp = tmp_path / "wt"
    wtp.mkdir()
    wt = GitWorktree(worktreePath=str(wtp), branchName="b")

    real_stat = os.stat

    def _stat(p, *a, **k):
        if str(p).endswith(".git"):
            raise PermissionError("denied .git")
        return real_stat(p, *a, **k)

    monkeypatch.setattr(worktree_git.os, "stat", _stat)
    with pytest.raises(RuntimeError) as ei:
        wt.IsValidWorktree()
    assert "failed to stat worktree .git" in str(ei.value)


# ---------------------------------------------------------------------------
# _decode
# ---------------------------------------------------------------------------
def test_decode_none_is_empty():
    assert worktree_git._decode(None) == ""


def test_decode_replaces_invalid_utf8():
    assert worktree_git._decode(b"a\xffb") == "a�b"
