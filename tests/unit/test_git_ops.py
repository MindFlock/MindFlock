"""Hermetic tests for backend/web/core/git_ops.py.

Two flavors of coverage:

* Pure command-construction / output-parsing tests that mock ``subprocess.run``
  (asserting the exact argv and the int/str/bool parsing of stdout).
* A handful of behavioral tests against a *real* throwaway git repo created in
  ``tmp_path`` (local git only, never the user's repo, never the network).

Anything that would hit the network (``ls-remote`` / ``origin/...``) is only
exercised via mocked subprocess so no real remote is contacted.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from backend.web.core import git_ops


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _cp(returncode=0, stdout=b"", stderr=b""):
    """A minimal stand-in for a CompletedProcess as the module reads it."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _cp_text(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def capture_run(monkeypatch):
    """Patch git_ops.subprocess.run; record calls and return a scripted result.

    Usage:
        capture_run.result = _cp(...)          # single fixed result
        capture_run.results = [cp1, cp2, ...]  # sequential results
    Recorded argv lists land in ``capture_run.calls``.
    """
    state = SimpleNamespace(calls=[], result=_cp(), results=None, kwargs=[])

    def fake_run(cmd, *args, **kwargs):
        state.calls.append(list(cmd))
        state.kwargs.append(kwargs)
        if state.results is not None:
            return state.results.pop(0)
        return state.result

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
    return state


# --------------------------------------------------------------------------- #
# _git_count                                                                   #
# --------------------------------------------------------------------------- #
def test_git_count_parses_int_and_builds_argv(capture_run):
    capture_run.result = _cp(returncode=0, stdout=b"7\n")
    assert git_ops._git_count("/wt", "main..HEAD") == 7
    assert capture_run.calls[0] == [
        "git",
        "-C",
        "/wt",
        "rev-list",
        "--count",
        "main..HEAD",
    ]


def test_git_count_returns_none_on_nonzero(capture_run):
    capture_run.result = _cp(returncode=1, stdout=b"")
    assert git_ops._git_count("/wt", "bogus..HEAD") is None


def test_git_count_returns_none_on_unparseable_stdout(capture_run):
    capture_run.result = _cp(returncode=0, stdout=b"not-a-number\n")
    assert git_ops._git_count("/wt", "x..y") is None


# --------------------------------------------------------------------------- #
# _commits_beyond_base                                                         #
# --------------------------------------------------------------------------- #
def test_commits_beyond_base_prefers_origin_range(capture_run):
    # First _git_count call (origin/main..HEAD) succeeds -> value used, no fallback.
    capture_run.results = [_cp(returncode=0, stdout=b"3\n")]
    assert git_ops._commits_beyond_base("/wt", "main") == 3
    assert capture_run.calls[0] == [
        "git",
        "-C",
        "/wt",
        "rev-list",
        "--count",
        "origin/main..HEAD",
    ]
    assert len(capture_run.calls) == 1  # did not fall through to local base


def test_commits_beyond_base_falls_back_to_local_base(capture_run):
    # origin/main invalid (rc=1 -> None), local main succeeds.
    capture_run.results = [
        _cp(returncode=1, stdout=b""),
        _cp(returncode=0, stdout=b"2\n"),
    ]
    assert git_ops._commits_beyond_base("/wt", "main") == 2
    assert capture_run.calls[0][-1] == "origin/main..HEAD"
    assert capture_run.calls[1][-1] == "main..HEAD"


def test_commits_beyond_base_defaults_to_zero(capture_run):
    capture_run.results = [
        _cp(returncode=1, stdout=b""),
        _cp(returncode=1, stdout=b""),
    ]
    assert git_ops._commits_beyond_base("/wt", "main") == 0


# --------------------------------------------------------------------------- #
# _has_upstream                                                                #
# --------------------------------------------------------------------------- #
def test_has_upstream_true_and_argv(capture_run):
    capture_run.result = _cp(returncode=0)
    assert git_ops._has_upstream("/wt") is True
    assert capture_run.calls[0] == [
        "git",
        "-C",
        "/wt",
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{u}",
    ]


def test_has_upstream_false_when_nonzero(capture_run):
    capture_run.result = _cp(returncode=128)
    assert git_ops._has_upstream("/wt") is False


# --------------------------------------------------------------------------- #
# _is_dirty                                                                    #
# --------------------------------------------------------------------------- #
def test_is_dirty_true_when_porcelain_nonempty(capture_run):
    capture_run.result = _cp(returncode=0, stdout=b" M file.py\n")
    assert git_ops._is_dirty("/wt") is True
    assert capture_run.calls[0] == [
        "git",
        "-C",
        "/wt",
        "status",
        "--porcelain",
    ]


def test_is_dirty_false_when_clean(capture_run):
    capture_run.result = _cp(returncode=0, stdout=b"")
    assert git_ops._is_dirty("/wt") is False


def test_is_dirty_false_on_error_returncode(capture_run):
    # nonzero rc -> not dirty even if stdout has bytes
    capture_run.result = _cp(returncode=1, stdout=b" M x\n")
    assert git_ops._is_dirty("/wt") is False


# --------------------------------------------------------------------------- #
# _git_head_sha                                                                #
# --------------------------------------------------------------------------- #
def test_git_head_sha_strips_and_decodes(capture_run):
    capture_run.result = _cp(returncode=0, stdout=b"deadbeef\n")
    assert git_ops._git_head_sha("/wt") == "deadbeef"
    assert capture_run.calls[0] == ["git", "-C", "/wt", "rev-parse", "HEAD"]


def test_git_head_sha_empty_on_error(capture_run):
    capture_run.result = _cp(returncode=1, stdout=b"whatever")
    assert git_ops._git_head_sha("/wt") == ""


# --------------------------------------------------------------------------- #
# _current_branch                                                              #
# --------------------------------------------------------------------------- #
def test_current_branch_parses_name_and_argv(capture_run):
    capture_run.result = _cp(returncode=0, stdout=b"feature/foo\n")
    assert git_ops._current_branch("/wt") == "feature/foo"
    assert capture_run.calls[0] == [
        "git",
        "-C",
        "/wt",
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
    ]


def test_current_branch_empty_when_detached(capture_run):
    capture_run.result = _cp(returncode=1, stdout=b"")
    assert git_ops._current_branch("/wt") == ""


# --------------------------------------------------------------------------- #
# _origin_branch_sha  (cache + network guards, all mocked)                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_origin_cache():
    git_ops._ORIGIN_SHA_CACHE.clear()
    git_ops._ORIGIN_SHA_PENDING.clear()
    yield
    git_ops._ORIGIN_SHA_CACHE.clear()
    git_ops._ORIGIN_SHA_PENDING.clear()


def test_origin_branch_sha_none_for_empty_branch(capture_run):
    assert git_ops._origin_branch_sha("/wt", "") is None
    assert capture_run.calls == []  # never shells out


def test_origin_branch_sha_parses_first_field(capture_run):
    capture_run.result = _cp(
        returncode=0,
        stdout=b"abc123\trefs/heads/main\n",
    )
    assert git_ops._origin_branch_sha("/wt", "main") == "abc123"
    assert capture_run.calls[0] == [
        "git",
        "-C",
        "/wt",
        "ls-remote",
        "--heads",
        "origin",
        "main",
    ]
    # a timeout is passed so the network call is bounded
    assert capture_run.kwargs[0].get("timeout") == 12


def test_origin_branch_sha_none_when_branch_absent(capture_run):
    capture_run.result = _cp(returncode=0, stdout=b"")
    assert git_ops._origin_branch_sha("/wt", "nope") is None


def test_origin_branch_sha_uses_cache_within_ttl(capture_run):
    capture_run.result = _cp(returncode=0, stdout=b"sha1\trefs/heads/main\n")
    first = git_ops._origin_branch_sha("/wt", "main")
    assert first == "sha1"
    # second call within TTL must be served from cache (no new subprocess)
    capture_run.result = _cp(returncode=0, stdout=b"sha2\trefs/heads/main\n")
    second = git_ops._origin_branch_sha("/wt", "main")
    assert second == "sha1"
    assert len(capture_run.calls) == 1


def test_origin_branch_sha_force_bypasses_cache(capture_run):
    capture_run.result = _cp(returncode=0, stdout=b"sha1\trefs/heads/main\n")
    assert git_ops._origin_branch_sha("/wt", "main") == "sha1"
    capture_run.result = _cp(returncode=0, stdout=b"sha2\trefs/heads/main\n")
    assert git_ops._origin_branch_sha("/wt", "main", force=True) == "sha2"
    assert len(capture_run.calls) == 2


def test_mark_origin_push_pending_bypasses_fresh_cache(capture_run):
    # A fresh cache entry would normally short-circuit the ls-remote...
    capture_run.result = _cp(returncode=0, stdout=b"old\trefs/heads/main\n")
    assert git_ops._origin_branch_sha("/wt", "main") == "old"
    assert len(capture_run.calls) == 1
    # ...but after a push is marked pending, the next call must re-query origin
    # (the push may not have landed when the cache was last populated).
    git_ops.mark_origin_push_pending("/wt", "main")
    capture_run.result = _cp(returncode=0, stdout=b"new\trefs/heads/main\n")
    assert git_ops._origin_branch_sha("/wt", "main") == "new"
    assert len(capture_run.calls) == 2


def test_mark_origin_push_pending_expires_and_reverts_to_cache(
    capture_run, monkeypatch
):
    capture_run.result = _cp(returncode=0, stdout=b"s\trefs/heads/main\n")
    # Mark pending in the past so the window is already elapsed on next lookup.
    git_ops.mark_origin_push_pending("/wt", "main")
    git_ops._ORIGIN_SHA_PENDING[("/wt", "main")] = git_ops.time.time() - 1
    # First call: window elapsed, so it queries once and re-caches...
    assert git_ops._origin_branch_sha("/wt", "main") == "s"
    # ...and the expired pending entry is cleaned up, so a follow-up call within
    # the cache TTL is served from cache (no extra subprocess).
    assert ("/wt", "main") not in git_ops._ORIGIN_SHA_PENDING
    git_ops._origin_branch_sha("/wt", "main")
    assert len(capture_run.calls) == 1


def test_mark_origin_push_pending_ignores_empty_branch():
    git_ops.mark_origin_push_pending("/wt", "")
    assert git_ops._ORIGIN_SHA_PENDING == {}


def test_origin_branch_sha_timeout_falls_back_to_cache(monkeypatch):
    # Seed the cache with a live entry, then make the refresh time out.
    git_ops._ORIGIN_SHA_CACHE[("/wt", "main")] = (
        git_ops.time.time() + 1000,
        "cached-sha",
    )

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=12)

    monkeypatch.setattr(git_ops.subprocess, "run", boom)
    # force=True bypasses the fresh-cache short-circuit, so run() is called and
    # the TimeoutExpired handler returns the previously cached value.
    assert git_ops._origin_branch_sha("/wt", "main", force=True) == "cached-sha"


def test_origin_branch_sha_timeout_no_cache_returns_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(git_ops.subprocess, "run", boom)
    assert git_ops._origin_branch_sha("/wt", "main") is None


# --------------------------------------------------------------------------- #
# _is_git_repo / _git_has_commits  (mocked argv + parsing)                     #
# --------------------------------------------------------------------------- #
def test_is_git_repo_true_and_argv(capture_run):
    capture_run.result = _cp_text(returncode=0, stdout="true\n")
    assert git_ops._is_git_repo("/some/path") is True
    assert capture_run.calls[0] == [
        "git",
        "-C",
        "/some/path",
        "rev-parse",
        "--is-inside-work-tree",
    ]
    assert capture_run.kwargs[0].get("text") is True


def test_is_git_repo_false_on_wrong_stdout(capture_run):
    capture_run.result = _cp_text(returncode=0, stdout="false\n")
    assert git_ops._is_git_repo("/x") is False


def test_is_git_repo_false_on_exception(monkeypatch):
    monkeypatch.setattr(
        git_ops.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")),
    )
    assert git_ops._is_git_repo("/x") is False


def test_git_has_commits_true(capture_run):
    capture_run.result = _cp_text(returncode=0, stdout="deadbeef\n")
    assert git_ops._git_has_commits("/x") is True
    assert capture_run.calls[0] == [
        "git",
        "-C",
        "/x",
        "rev-parse",
        "--verify",
        "HEAD",
    ]


def test_git_has_commits_false_on_nonzero(capture_run):
    capture_run.result = _cp_text(returncode=128, stdout="")
    assert git_ops._git_has_commits("/x") is False


# --------------------------------------------------------------------------- #
# _make_initial_commit  (mocked: identity injection logic)                     #
# --------------------------------------------------------------------------- #
def test_make_initial_commit_injects_identity_when_missing(capture_run):
    # config user.email -> empty, config user.name -> empty, then commit ok
    capture_run.results = [
        _cp_text(returncode=0, stdout="\n"),  # user.email empty
        _cp_text(returncode=0, stdout="\n"),  # user.name empty
        _cp_text(returncode=0, stdout=""),  # commit
    ]
    git_ops._make_initial_commit("/repo")
    commit_argv = capture_run.calls[2]
    assert "-c" in commit_argv
    assert "user.email=mindflock@localhost" in commit_argv
    assert "user.name=MindFlock" in commit_argv
    assert commit_argv[-3:] == ["commit", "--allow-empty", "-m"] or commit_argv[
        -4:
    ] == ["commit", "--allow-empty", "-m", "Initial commit"]


def test_make_initial_commit_preserves_existing_identity(capture_run):
    capture_run.results = [
        _cp_text(returncode=0, stdout="me@example.com\n"),  # user.email present
        _cp_text(returncode=0, stdout="Me\n"),  # user.name present
        _cp_text(returncode=0, stdout=""),  # commit
    ]
    git_ops._make_initial_commit("/repo")
    commit_argv = capture_run.calls[2]
    assert "user.email=mindflock@localhost" not in commit_argv
    assert commit_argv == [
        "git",
        "-C",
        "/repo",
        "commit",
        "--allow-empty",
        "-m",
        "Initial commit",
    ]


def test_make_initial_commit_raises_on_commit_failure(capture_run):
    capture_run.results = [
        _cp_text(returncode=0, stdout="\n"),  # user.email empty
        _cp_text(returncode=0, stdout="\n"),  # user.name empty
        _cp_text(returncode=1, stdout="", stderr="boom"),  # commit fails
    ]
    with pytest.raises(ValueError, match="failed to create initial commit: boom"):
        git_ops._make_initial_commit("/repo")


# --------------------------------------------------------------------------- #
# Integration against a REAL throwaway local git repo in tmp_path              #
# --------------------------------------------------------------------------- #
def _run_git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def real_repo(tmp_path):
    """A local git repo with one commit on branch 'main'; identity set locally."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    _run_git(repo, "config", "user.email", "t@t.local")
    _run_git(repo, "config", "user.name", "Tester")
    (repo / "a.txt").write_text("hello\n")
    _run_git(repo, "add", "a.txt")
    _run_git(repo, "commit", "-m", "first")
    return repo


def test_real_is_git_repo_and_has_commits(real_repo, tmp_path):
    assert git_ops._is_git_repo(str(real_repo)) is True
    assert git_ops._git_has_commits(str(real_repo)) is True
    # a plain directory is not a repo
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_ops._is_git_repo(str(plain)) is False


def test_real_current_branch_and_head_sha(real_repo):
    assert git_ops._current_branch(str(real_repo)) == "main"
    sha = git_ops._git_head_sha(str(real_repo))
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_real_is_dirty_transitions(real_repo):
    assert git_ops._is_dirty(str(real_repo)) is False
    (real_repo / "a.txt").write_text("changed\n")
    assert git_ops._is_dirty(str(real_repo)) is True


def test_real_has_no_upstream(real_repo):
    # No remote configured -> no upstream.
    assert git_ops._has_upstream(str(real_repo)) is False


def test_real_commits_beyond_base(real_repo):
    # HEAD == main so main..HEAD counts 0; add a commit on a feature branch.
    assert git_ops._commits_beyond_base(str(real_repo), "main") == 0
    _run_git(real_repo, "checkout", "-b", "feat")
    (real_repo / "b.txt").write_text("b\n")
    _run_git(real_repo, "add", "b.txt")
    _run_git(real_repo, "commit", "-m", "second")
    # origin/main doesn't exist -> falls back to local 'main' -> 1 commit ahead
    assert git_ops._commits_beyond_base(str(real_repo), "main") == 1


def test_real_git_count_invalid_range_returns_none(real_repo):
    assert git_ops._git_count(str(real_repo), "no-such-ref..HEAD") is None


def test_real_make_initial_commit_on_empty_repo(tmp_path, monkeypatch):
    # Isolate git from the real user's global identity so the "inject throwaway
    # identity" branch is exercised deterministically. Point every config layer
    # at empty files inside tmp_path and drop any GIT_* identity env vars.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    empty_cfg = tmp_path / "gitconfig-none"
    empty_cfg.write_text("")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty_cfg))
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)

    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    # No identity configured anywhere; module should inject its throwaway one.
    assert git_ops._git_has_commits(str(repo)) is False
    git_ops._make_initial_commit(str(repo))
    assert git_ops._git_has_commits(str(repo)) is True
    author = _run_git(repo, "log", "-1", "--format=%ae").stdout.strip()
    assert author == "mindflock@localhost"
