"""A PR review starts like a ticket: off the repo's shared base clone.

``pr_provisioner.prepare_pr_worktree`` fetches ``pull/<n>/head`` into the base
clone (``workspaces/_base_<repo>``) and points a local branch named exactly
``head_ref`` at it; the engine's worktree strategy then adds the session's
worktree from that existing branch. These tests drive the real git path against
a local "forge" whose PRs are plain ``refs/pull/<n>/head`` refs, the shape
GitHub serves for same-repo and fork PRs alike.
"""

from __future__ import annotations

import asyncio
import subprocess
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.session import provisioned
from backend.ticket_ingestion import pr_provisioner
from backend.ticket_ingestion.models import PullRequest
from backend.ticket_ingestion.pr_provisioner import (
    PRWorktreeUnavailable,
    prepare_pr_worktree,
)


def _git(repo, *args) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return cp.stdout.strip()


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", "add " + name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A bare forge with ``main`` + ``develop``, a contributor checkout to make
    PR commits in, and provisioning settings whose workspaces live in tmp."""
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path / "home"))
    forge = tmp_path / "forge.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(forge)], check=True
    )
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(forge), str(dev)], check=True)
    _git(dev, "config", "user.email", "t@example.com")
    _git(dev, "config", "user.name", "t")
    _git(dev, "checkout", "-q", "-b", "main")
    _commit(dev, "README", "base\n")
    _git(dev, "push", "-q", "origin", "main")
    _git(dev, "checkout", "-q", "-b", "develop")
    _commit(dev, "DEVELOP", "develop\n")
    _git(dev, "push", "-q", "origin", "develop")
    _git(dev, "checkout", "-q", "main")

    settings = provisioned.ProvisionSettings(
        repo_url=str(forge),
        workspace_dir=tmp_path / "ws",
        base_branch="main",
        setup_commands=[],
    )
    monkeypatch.setattr(
        provisioned, "load_provision_settings", lambda *a, **k: settings
    )
    setups: list = []
    monkeypatch.setattr(
        provisioned,
        "provision_workspace",
        lambda d, b, s, **k: setups.append((str(d), b)),
    )
    return types.SimpleNamespace(
        forge=forge, dev=dev, settings=settings, setups=setups, tmp=tmp_path
    )


def _open_pr(w, number: int, head_ref: str, *, base="main", fork=False) -> str:
    """Commit a PR head on ``base`` and publish it as ``refs/pull/<n>/head``.

    A same-repo PR also pushes the branch; a fork PR's commit exists on the
    forge ONLY under ``refs/pull/<n>/head``, as GitHub serves it."""
    _git(w.dev, "checkout", "-q", "-B", "pr-%d" % number, "origin/" + base)
    sha = _commit(w.dev, "pr%d.txt" % number, "change %d\n" % number)
    _git(w.dev, "push", "-q", "-f", "origin", "HEAD:refs/pull/%d/head" % number)
    if not fork:
        _git(w.dev, "push", "-q", "-f", "origin", "HEAD:refs/heads/" + head_ref)
    _git(w.dev, "checkout", "-q", "main")
    return sha


def _pr(number, head_ref, sha, base="main") -> PullRequest:
    return PullRequest(
        number=number,
        head_ref=head_ref,
        head_sha=sha,
        base_ref=base,
        title="t",
        url="u",
        author="a",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        repo="o/forge",
        clone_url="",
    )


def _session(w, pr, base_branch=""):
    """What the engine does at Start for a worktree-strategy review session."""
    wt = provisioned.build_provisioned_worktree(
        "worktree",
        pr.head_ref,
        "pr-forge-%d" % pr.number,
        w.settings,
        base_branch=base_branch,
    )
    wt.Setup()
    return wt


def test_fresh_pr_is_a_worktree_of_the_base_clone(world):
    sha = _open_pr(world, 7, "feature/x")
    got = prepare_pr_worktree(_pr(7, "feature/x", sha), repo_url=str(world.forge))
    base = provisioned.resolve_base_repo_dir(world.settings)
    assert got.base_repo == str(base) and got.head_sha == sha
    assert _git(base, "rev-parse", "refs/heads/feature/x") == sha
    assert _git(base, "rev-parse", "refs/mindflock/pr/7") == sha

    wt = _session(world, _pr(7, "feature/x", sha))
    assert wt.worktreePath.startswith(str(world.tmp / "home" / "worktrees"))
    assert _git(wt.worktreePath, "rev-parse", "HEAD") == sha
    assert _git(wt.worktreePath, "symbolic-ref", "--short", "HEAD") == "feature/x"
    # Diff base: the fork point on main, not the PR head itself.
    assert wt.baseCommitSHA == _git(base, "merge-base", sha, "main")
    # Setup commands / cache seeds: exactly once, in the session's worktree.
    assert world.setups == [(wt.worktreePath, "feature/x")]


def test_fork_pr_head_only_exists_under_refs_pull(world):
    sha = _open_pr(world, 8, "patch-1", fork=True)
    with pytest.raises(subprocess.CalledProcessError):
        _git(world.forge, "rev-parse", "--verify", "refs/heads/patch-1")
    prepare_pr_worktree(_pr(8, "patch-1", sha))
    wt = _session(world, _pr(8, "patch-1", sha))
    assert _git(wt.worktreePath, "rev-parse", "HEAD") == sha


def test_branch_name_with_uppercase_and_slashes_stays_exact(world):
    sha = _open_pr(world, 9, "Feature/Fix-Login")
    prepare_pr_worktree(_pr(9, "Feature/Fix-Login", sha))
    wt = _session(world, _pr(9, "Feature/Fix-Login", sha))
    assert wt.branchName == "Feature/Fix-Login"
    assert (
        _git(wt.worktreePath, "symbolic-ref", "--short", "HEAD") == "Feature/Fix-Login"
    )
    base = provisioned.resolve_base_repo_dir(world.settings)
    with pytest.raises(subprocess.CalledProcessError):
        _git(base, "rev-parse", "--verify", "refs/heads/feature/fix-login")


def test_new_branches_are_still_sanitized(world):
    # Only an EXISTING branch is used as spelled; a new one is cleaned up.
    wt = provisioned.build_provisioned_worktree(
        "worktree", "Feature/New Thing", "s", world.settings
    )
    assert wt.branchName == "feature/new-thing"


def test_pr_into_another_branch_diffs_against_its_base(world):
    sha = _open_pr(world, 10, "feature/on-develop", base="develop")
    pr = _pr(10, "feature/on-develop", sha, base="develop")
    got = prepare_pr_worktree(pr)
    assert got.base_branch == "develop"
    base = provisioned.resolve_base_repo_dir(world.settings)
    # The base clone is single-branch (main); the fetch brought develop too.
    develop = _git(base, "rev-parse", "refs/remotes/origin/develop")
    assert provisioned.recorded_base_branch(base, pr.head_ref) == "develop"
    # Recorded in the repo config, so it holds with no override (a restart).
    wt = _session(world, pr)
    assert wt.baseCommitSHA == develop


def test_re_review_after_kill_refreshes_to_the_new_head(world):
    sha = _open_pr(world, 7, "feature/x")
    prepare_pr_worktree(_pr(7, "feature/x", sha))
    first = _session(world, _pr(7, "feature/x", sha))
    first.Cleanup()  # kill: worktree removed, branch deleted

    newer = _open_pr(world, 7, "feature/x")  # the author pushed again
    got = prepare_pr_worktree(_pr(7, "feature/x", newer))
    assert got.head_sha == newer
    second = _session(world, _pr(7, "feature/x", newer))
    assert _git(second.worktreePath, "rev-parse", "HEAD") == newer


def test_re_review_reclaims_a_closed_sessions_clean_worktree(world):
    sha = _open_pr(world, 7, "feature/x")
    prepare_pr_worktree(_pr(7, "feature/x", sha))
    kept = _session(world, _pr(7, "feature/x", sha))  # closed, worktree kept

    newer = _open_pr(world, 7, "feature/x")
    got = prepare_pr_worktree(_pr(7, "feature/x", newer), is_live=lambda p: False)
    assert got.reclaimed == kept.worktreePath
    assert not Path(kept.worktreePath).exists()  # nothing was in it but the PR
    again = _session(world, _pr(7, "feature/x", newer))
    assert _git(again.worktreePath, "rev-parse", "HEAD") == newer


def test_re_review_keeps_a_closed_sessions_local_work(world):
    sha = _open_pr(world, 7, "feature/x")
    prepare_pr_worktree(_pr(7, "feature/x", sha))
    kept = _session(world, _pr(7, "feature/x", sha))
    Path(kept.worktreePath, "pr7.txt").write_text("the reviewer's unstaged fix\n")

    got = prepare_pr_worktree(_pr(7, "feature/x", sha), is_live=lambda p: False)
    assert got.reclaimed == kept.worktreePath
    # Detached in place: the fix is still there, the branch is free.
    assert Path(kept.worktreePath, "pr7.txt").read_text().startswith("the reviewer")
    detached = subprocess.run(
        ["git", "-C", kept.worktreePath, "symbolic-ref", "-q", "HEAD"],
        stdout=subprocess.PIPE,
    )
    assert detached.returncode != 0
    again = _session(world, _pr(7, "feature/x", sha))
    assert _git(again.worktreePath, "rev-parse", "HEAD") == sha


def test_a_live_sessions_branch_is_not_taken(world):
    sha = _open_pr(world, 7, "feature/x")
    prepare_pr_worktree(_pr(7, "feature/x", sha))
    live = _session(world, _pr(7, "feature/x", sha))
    with pytest.raises(PRWorktreeUnavailable, match="live session"):
        prepare_pr_worktree(
            _pr(7, "feature/x", sha), is_live=lambda p: p == live.worktreePath
        )
    assert Path(live.worktreePath).exists()


def test_the_base_clones_own_branch_falls_back(world):
    # A fork PR from its `main` into main: the base clone has main checked out.
    sha = _open_pr(world, 11, "main", fork=True)
    provisioned.ensure_base_repo(world.settings)
    with pytest.raises(PRWorktreeUnavailable, match="base clone"):
        prepare_pr_worktree(_pr(11, "main", sha))


def test_no_settings_and_no_pr_ref_fall_back(world, monkeypatch):
    with pytest.raises(PRWorktreeUnavailable, match="fetch"):
        prepare_pr_worktree(_pr(99, "feature/none", "0" * 40))
    monkeypatch.setattr(provisioned, "load_provision_settings", lambda *a, **k: None)
    with pytest.raises(PRWorktreeUnavailable, match="no repository"):
        prepare_pr_worktree(_pr(7, "feature/x", "0" * 40))


def test_a_base_clone_of_another_repo_is_never_used(world, monkeypatch):
    sha = _open_pr(world, 7, "feature/x")
    with pytest.raises(PRWorktreeUnavailable, match="tracks"):
        prepare_pr_worktree(
            _pr(7, "feature/x", sha),
            repo_url="https://github.com/someone-else/forge.git",
        )


def test_clone_fallback_runs_setup_once(world, monkeypatch):
    """The fallback clone is provisioned by the session's Setup, not twice."""
    from backend.ticket_ingestion.config import PipelineConfig

    sha = _open_pr(world, 7, "Feature/X", base="develop")
    pr = _pr(7, "Feature/X", sha, base="develop")
    pr.clone_url = str(world.forge)
    ran: list = []

    async def _setup(*a, **k):
        ran.append(a)

    monkeypatch.setattr(pr_provisioner, "run_setup_commands_async", _setup)
    cfg = PipelineConfig(repo_url=str(world.forge), workspace_dir=world.tmp / "ws")
    ws = asyncio.run(
        pr_provisioner.PRProvisioner(cfg).provision(
            pr, launch_cursor=False, run_setup=False
        )
    )
    assert ran == []
    wt = provisioned.build_provisioned_worktree(
        "clone",
        pr.head_ref,
        "pr-forge-7",
        world.settings,
        workspace_path=str(ws.directory),
    )
    wt.Setup()
    assert world.setups == [(str(ws.directory), "Feature/X")]
    assert wt.baseCommitSHA == _git(ws.directory, "rev-parse", "origin/develop")


def test_list_probe_finds_the_review_worktree(world, monkeypatch):
    from backend.web.core import pr_review

    sha = _open_pr(world, 7, "Feature/X")
    pr = _pr(7, "Feature/X", sha)
    prepare_pr_worktree(pr)
    wt = _session(world, pr)
    monkeypatch.setattr(pr_review, "review_repo_url", lambda pr, cfg: "")
    other = _pr(8, "nobody/has-this", "0" * 40)
    paths = pr_review.review_workspace_paths(
        [pr, other], types.SimpleNamespace(), str(world.tmp / "ws")
    )
    assert paths[("o/forge", 7)] == wt.worktreePath
    # Nothing on disk: the would-be clone path, which the probe finds missing.
    assert paths[("o/forge", 8)] == str(world.tmp / "ws" / "pr-forge-8")


def test_new_instance_carries_the_prs_base_branch(world):
    """``InstanceOptions.base_branch`` reaches a provisioned session: its
    BaseBranch (stage / committed checks) and its worktree's diff base."""
    from backend import session

    sha = _open_pr(world, 12, "Feature/Y", base="develop")
    prepare_pr_worktree(_pr(12, "Feature/Y", sha, base="develop"))
    inst = session.NewInstance(
        session.InstanceOptions(
            title="pr-forge-12",
            path=".",
            program="claude",
            provisioned=True,
            workspace_strategy="worktree",
            new_branch="Feature/Y",
            base_branch="develop",
        )
    )
    inst._create_first_time_worktree()
    assert inst.Branch == "Feature/Y"
    assert inst.BaseBranch == "develop"
    assert inst._git_worktree._base_branch == "develop"
    # A plain provisioned session (no override) keeps the configured base.
    plain = session.NewInstance(
        session.InstanceOptions(title="t", path=".", program="claude", provisioned=True)
    )
    plain._create_first_time_worktree()
    assert plain.BaseBranch == "main"


def test_a_branch_with_commits_of_its_own_is_never_moved(world):
    """A paused session's branch (no worktree while paused) with unpushed
    work keeps it: the review falls back instead of moving the branch."""
    sha = _open_pr(world, 7, "feature/x")
    prepare_pr_worktree(_pr(7, "feature/x", sha))
    paused = _session(world, _pr(7, "feature/x", sha))
    _git(paused.worktreePath, "config", "user.email", "t@example.com")
    _git(paused.worktreePath, "config", "user.name", "t")
    mine = _commit(Path(paused.worktreePath), "local.txt", "unpushed\n")
    paused.Remove()  # Pause: worktree gone, branch kept

    with pytest.raises(PRWorktreeUnavailable, match="not in the PR"):
        prepare_pr_worktree(_pr(7, "feature/x", sha), is_live=lambda p: False)
    base = provisioned.resolve_base_repo_dir(world.settings)
    assert _git(base, "rev-parse", "refs/heads/feature/x") == mine


def test_re_review_after_a_force_push_reclaims_the_old_worktree(world):
    sha = _open_pr(world, 7, "feature/x")
    prepare_pr_worktree(_pr(7, "feature/x", sha))
    kept = _session(world, _pr(7, "feature/x", sha))  # closed, worktree kept

    # The author rewrote the PR: the old head is not in the new one.
    _git(world.dev, "checkout", "-q", "-B", "rewrite", "origin/main")
    rewritten = _commit(world.dev, "pr7.txt", "rewritten\n")
    _git(world.dev, "push", "-q", "-f", "origin", "HEAD:refs/pull/7/head")
    _git(world.dev, "checkout", "-q", "main")

    got = prepare_pr_worktree(_pr(7, "feature/x", rewritten), is_live=lambda p: False)
    assert got.head_sha == rewritten
    assert not Path(kept.worktreePath).exists()
    again = _session(world, _pr(7, "feature/x", rewritten))
    assert _git(again.worktreePath, "rev-parse", "HEAD") == rewritten
