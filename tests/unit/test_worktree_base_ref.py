"""``base_ref``: a new session's worktree cut from a given commit, not HEAD.

The point of the feature: a worker spawned by an orchestrator agent forks from
the ORCHESTRATOR's commit (which lives on the orchestrator's own branch, in the
same repo) while its ``Path`` stays the canonical repo root — so cleanup never
depends on the orchestrator's worktree. These run against real throwaway git
repos under ``tmp_path`` with ``$HOME`` redirected, so the worktrees directory
(``$HOME/.mindflock/worktrees``) is in tmp too.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from backend.session import instance as inst_mod
from backend.session.git.worktree import GitWorktree, get_worktree_directory
from backend.session.git.worktree import new_git_worktree
from backend.session.instance import InstanceOptions, new_instance


def _run(cmd, cwd):
    res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert res.returncode == 0, res.stdout.decode("utf-8", "replace")
    return res.stdout.decode("utf-8", "replace")


def _commit(path, name, text):
    with open(os.path.join(path, name), "w") as fh:
        fh.write(text)
    _run(["git", "add", "."], cwd=path)
    _run(["git", "commit", "-q", "-m", "add " + name], cwd=path)
    return _run(["git", "rev-parse", "HEAD"], cwd=path).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo on ``main`` plus a side branch ``orch`` one commit ahead, which
    is NOT checked out (HEAD stays on main) — the orchestrator's commit."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = str(tmp_path / "repo")
    os.makedirs(path)
    _run(["git", "init", "-q", "-b", "main"], cwd=path)
    _run(["git", "config", "user.email", "t@example.com"], cwd=path)
    _run(["git", "config", "user.name", "T"], cwd=path)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=path)
    main_sha = _commit(path, "README.md", "hello\n")
    _run(["git", "switch", "-q", "-c", "orch"], cwd=path)
    orch_sha = _commit(path, "orch.txt", "orchestrator work\n")
    _run(["git", "switch", "-q", "main"], cwd=path)
    return {"path": path, "main": main_sha, "orch": orch_sha}


def _wt(repo_path, branch, base_ref=""):
    return GitWorktree(
        repoPath=repo_path,
        worktreePath=os.path.join(get_worktree_directory(), branch + "_wt"),
        sessionName="sess",
        branchName=branch,
        baseRef=base_ref,
    )


def _sha_at(path, rev="HEAD"):
    return _run(["git", "rev-parse", rev], cwd=path).strip()


# --------------------------------------------------------------------------- #
# GitWorktree.Setup with baseRef                                              #
# --------------------------------------------------------------------------- #
def test_setup_without_base_ref_still_cuts_from_head(repo):
    wt = _wt(repo["path"], "plain")
    wt.Setup()
    assert _sha_at(wt.GetWorktreePath()) == repo["main"]
    assert wt.GetBaseCommitSHA() == repo["main"]


def test_setup_cuts_the_branch_from_a_sha(repo):
    wt = _wt(repo["path"], "worker", base_ref=repo["orch"])
    wt.Setup()
    assert _sha_at(wt.GetWorktreePath()) == repo["orch"]
    assert os.path.isfile(os.path.join(wt.GetWorktreePath(), "orch.txt"))
    # The diff base is the fork point, not the repo's HEAD.
    assert wt.GetBaseCommitSHA() == repo["orch"]
    # The branch is the session's own, at the fork point.
    assert _sha_at(repo["path"], "refs/heads/worker") == repo["orch"]


def test_setup_resolves_a_branch_name_and_a_short_sha(repo):
    by_name = _wt(repo["path"], "w-name", base_ref="orch")
    by_name.Setup()
    assert by_name.GetBaseCommitSHA() == repo["orch"]
    by_short = _wt(repo["path"], "w-short", base_ref=repo["orch"][:10])
    by_short.Setup()
    # Recorded as the FULL sha, so a later ref move cannot change the base.
    assert by_short.GetBaseCommitSHA() == repo["orch"]


def test_base_ref_is_consumed_so_a_resume_reuses_the_branch(repo):
    wt = _wt(repo["path"], "resumable", base_ref=repo["orch"])
    wt.Setup()
    assert wt.baseRef == ""
    # Pause removes the worktree and keeps the branch; Resume calls Setup again
    # and must take the existing-branch path, not refuse the existing branch.
    _run(["git", "commit", "-q", "--allow-empty", "-m", "work"], cwd=wt.worktreePath)
    work_sha = _sha_at(wt.worktreePath)
    wt.Remove()
    wt.Setup()
    assert _sha_at(wt.worktreePath) == work_sha


def test_setup_refuses_an_existing_branch_with_a_base_ref(repo):
    _run(["git", "branch", "taken", repo["main"]], cwd=repo["path"])
    wt = _wt(repo["path"], "taken", base_ref=repo["orch"])
    with pytest.raises(RuntimeError, match="already exists"):
        wt.Setup()
    # Nothing was destroyed: the pre-existing branch is untouched.
    assert _sha_at(repo["path"], "refs/heads/taken") == repo["main"]
    assert not os.path.exists(wt.worktreePath)


def test_setup_unknown_base_ref_raises_and_leaves_no_branch(repo):
    wt = _wt(repo["path"], "ghost", base_ref="no-such-ref")
    with pytest.raises(RuntimeError, match="failed to resolve base ref no-such-ref"):
        wt.Setup()
    probe = subprocess.run(
        ["git", "-C", repo["path"], "show-ref", "--verify", "refs/heads/ghost"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert probe.returncode != 0
    assert not os.path.exists(wt.worktreePath)


def test_setup_refuses_an_option_shaped_base_ref(repo):
    wt = _wt(repo["path"], "opt", base_ref="--output=/tmp/x")
    with pytest.raises(RuntimeError, match="invalid base ref"):
        wt.Setup()


def test_new_git_worktree_carries_base_ref(repo):
    tree, branch = new_git_worktree(repo["path"], "kid", base_ref="orch")
    assert tree.baseRef == "orch"
    assert branch.endswith("kid")
    plain, _ = new_git_worktree(repo["path"], "kid2")
    assert plain.baseRef == ""


# --------------------------------------------------------------------------- #
# Instance: options -> worktree + recorded BaseBranch                         #
# --------------------------------------------------------------------------- #
def _cut(repo_path, title, **opts):
    inst = new_instance(
        InstanceOptions(title=title, path=repo_path, program="bash", **opts)
    )
    inst._create_first_time_worktree()
    inst._git_worktree.Setup()
    return inst


def test_instance_forks_from_base_ref_and_records_the_named_branch(repo):
    inst = _cut(repo["path"], "w1", base_ref=repo["orch"], base_branch="orch")
    wt = inst._git_worktree
    assert _sha_at(wt.GetWorktreePath()) == repo["orch"]
    assert inst.BaseBranch == "orch"
    # Path stays the canonical repo root — cleanup never depends on the
    # orchestrator's worktree.
    assert inst.Path == os.path.abspath(repo["path"])
    assert wt.GetRepoPath() == os.path.realpath(repo["path"])
    data = inst.ToInstanceData()
    assert data.base_branch == "orch"
    assert data.worktree.base_commit_sha == repo["orch"]


def test_instance_infers_base_branch_from_a_branch_ref(repo):
    inst = _cut(repo["path"], "w2", base_ref="orch")
    assert inst.BaseBranch == "orch"


def test_instance_sha_ref_without_base_branch_keeps_the_repo_branch(repo):
    inst = _cut(repo["path"], "w3", base_ref=repo["orch"])
    # A bare sha names no branch: fall back to the K1 default.
    assert inst.BaseBranch == "main"


def test_instance_without_base_ref_is_unchanged(repo):
    inst = _cut(repo["path"], "w4")
    assert inst.BaseBranch == "main"
    assert _sha_at(inst._git_worktree.GetWorktreePath()) == repo["main"]


def test_base_branch_alone_is_ignored_by_the_engine(repo):
    # Only meaningful with a fork point (the route 400s it on its own).
    inst = _cut(repo["path"], "w5", base_branch="orch")
    assert inst.BaseBranch == "main"


def test_is_local_branch(repo):
    assert inst_mod._is_local_branch(repo["path"], "orch") is True
    assert inst_mod._is_local_branch(repo["path"], repo["orch"]) is False
    assert inst_mod._is_local_branch(repo["path"], "") is False
    assert inst_mod._is_local_branch(repo["path"], "-x") is False
    assert inst_mod._is_local_branch(str(repo["path"]) + "-missing", "orch") is False


def test_refused_existing_branch_survives_the_failed_start_cleanup(repo):
    # Regression: the refusal used to leave isExistingBranch False, so
    # Instance.Start's failure path (_cleanup_partial -> Cleanup) ran
    # `git branch -D` on the branch it had just refused to reuse — deleting a
    # closed/paused namesake's unique commits.
    _run(["git", "branch", "precious", repo["main"]], cwd=repo["path"])
    _run(["git", "switch", "-q", "precious"], cwd=repo["path"])
    precious = _commit(repo["path"], "precious.txt", "precious work\n")
    _run(["git", "switch", "-q", "main"], cwd=repo["path"])
    wt = _wt(repo["path"], "precious", base_ref=repo["orch"])
    with pytest.raises(RuntimeError, match="already exists"):
        wt.Setup()
    try:
        wt.Cleanup()
    except Exception:  # noqa: BLE001 — nothing to remove is fine
        pass
    assert _sha_at(repo["path"], "refs/heads/precious") == precious
