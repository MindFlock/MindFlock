"""The stage pill follows the actual user flow across branch switches.

A workspace (in-place session) used for several branches in series must not
carry one branch's flow leftovers onto the next:

  * branch drift clears the stale commit-status marker (no false "interrupt")
    and the branch-scoped caches, and updates the displayed branch for
    in-place sessions;
  * a non-zero commit status self-heals once the tree is clean;
  * in-place sessions measure their stage against the repo's default branch
    (resolved live), not the branch the session happened to be created on;
  * merge / push endpoints target the LIVE branch and reset the flow.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.session.storage import Status
from backend.web import server
from backend.web.core import agent_state


# --------------------------------------------------------------------------- #
# helpers (same shapes as test_wave4_backend.py)
# --------------------------------------------------------------------------- #
def _git(*args: str, cwd: str) -> str:
    cp = subprocess.run(
        ["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout
    return cp.stdout.strip()


def _init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    (path / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=str(path))
    _git("commit", "-q", "-m", "init", cwd=str(path))
    return path


class _FakeInst:
    Program = "bash"
    Path = ""

    def __init__(
        self,
        wt: str,
        *,
        base_branch: str = "",
        branch: str = "",
        started: bool = True,
        status: Status = Status.Running,
        title: str = "t",
        in_place: bool = False,
    ):
        self.Title = title
        self.Branch = branch
        self.BaseBranch = base_branch
        self.Status = status
        self.InPlace = in_place
        self._wt = wt
        self._started = started

    def Started(self):  # noqa: N802
        return self._started

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    for cache in (
        server._BASE_BRANCH_CACHE,
        server._DIFF_STAT_CACHE,
        server._PR_CACHE,
        server._ORIGIN_SHA_CACHE,
        server._PROBE_CACHE,
        agent_state._LAST_BRANCH,
    ):
        cache.clear()
    # The interrupt path shells out to tmux for the failed-step text.
    monkeypatch.setattr(server, "_failed_precommit_step", lambda title: "hook")
    # No gh in unit tests.
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: None)
    yield
    for cache in (
        server._BASE_BRANCH_CACHE,
        server._DIFF_STAT_CACHE,
        server._PR_CACHE,
        server._ORIGIN_SHA_CACHE,
        server._PROBE_CACHE,
        agent_state._LAST_BRANCH,
    ):
        cache.clear()


def _write_status(wt: Path, code: str = "1") -> Path:
    # Production excludes the marker files from git (_exclude_artifacts);
    # mirror that so the marker itself doesn't read as a dirty tree.
    exclude = wt / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude, "a") as f:
        f.write(".mindflock_*\n")
    p = wt / ".mindflock_commit_status"
    p.write_text(code + "\n")
    return p


# --------------------------------------------------------------------------- #
# drift detection
# --------------------------------------------------------------------------- #
def test_drift_clears_stale_interrupt(tmp_path):
    wt = _init_repo(tmp_path / "r")
    inst = _FakeInst(str(wt), branch="main", in_place=True)
    status = _write_status(wt)
    (wt / "a.txt").write_text("dirty\n")

    # On the original branch the failed status legitimately shows interrupt.
    assert server._session_stage(inst)["stage"] == "interrupt"

    # Switch branches (still dirty): the old failure must not follow.
    _git("checkout", "-q", "-b", "feat-b", cwd=str(wt))
    res = server._session_stage(inst)
    assert res["stage"] == "agent"
    assert not status.exists()
    assert "failed_step" not in res


def test_first_observation_drift_updates_inplace_branch(tmp_path):
    # Server restart: _LAST_BRANCH is empty but the stored branch says "old".
    wt = _init_repo(tmp_path / "r")
    _git("checkout", "-q", "-b", "new", cwd=str(wt))
    status = _write_status(wt)
    inst = _FakeInst(str(wt), branch="old", in_place=True)

    server._session_stage(inst)

    assert inst.Branch == "new"  # sidebar label + merge fallback live
    assert not status.exists()
    assert agent_state._LAST_BRANCH[inst.Title] == "new"


def test_drift_never_rewrites_managed_session_branch(tmp_path):
    wt = _init_repo(tmp_path / "r")
    _git("checkout", "-q", "-b", "new", cwd=str(wt))
    status = _write_status(wt)
    server._PR_CACHE["old"] = (0, None)
    inst = _FakeInst(str(wt), branch="old", in_place=False)

    server._session_stage(inst)

    assert inst.Branch == "old"  # managed worktrees keep identity
    assert not status.exists()  # ...but the flow artifacts still reset
    assert "old" not in server._PR_CACHE


def test_detached_head_is_not_drift(tmp_path):
    wt = _init_repo(tmp_path / "r")
    inst = _FakeInst(str(wt), branch="main", in_place=True)
    server._session_stage(inst)
    assert agent_state._LAST_BRANCH[inst.Title] == "main"

    _git("checkout", "-q", "--detach", cwd=str(wt))
    status = _write_status(wt)
    (wt / "a.txt").write_text("dirty\n")

    assert server._session_stage(inst)["stage"] == "interrupt"
    assert agent_state._LAST_BRANCH[inst.Title] == "main"  # not updated
    assert status.exists()


def test_clean_tree_self_heals_status_file(tmp_path):
    wt = _init_repo(tmp_path / "r")
    inst = _FakeInst(str(wt), branch="main")
    status = _write_status(wt)  # non-zero, but the tree is clean

    res = server._session_stage(inst)

    assert res["stage"] != "interrupt"
    assert not status.exists()


# --------------------------------------------------------------------------- #
# in-place live base
# --------------------------------------------------------------------------- #
def test_inplace_base_ignores_frozen_base_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_origin_branch_sha", lambda wt, b: "")
    wt = _init_repo(tmp_path / "r")  # default branch: main
    _git("checkout", "-q", "-b", "feat-b", cwd=str(wt))
    (wt / "b.txt").write_text("two\n")
    _git("add", "-A", cwd=str(wt))
    _git("commit", "-q", "-m", "work", cwd=str(wt))

    # Frozen base says feat-a (a previous task); live resolution must say main.
    inst = _FakeInst(str(wt), base_branch="feat-a", branch="feat-b", in_place=True)
    assert server._session_base_branch(inst) == "main"
    assert server._session_stage(inst)["stage"] == "committed"

    # Managed sessions keep trusting the stored value.
    managed = _FakeInst(str(wt), base_branch="feat-a", branch="feat-b")
    assert server._session_base_branch(managed) == "feat-a"


# --------------------------------------------------------------------------- #
# PR detection is by head branch alone (make-pr can target any base)
# --------------------------------------------------------------------------- #
def test_open_pr_advances_chip_and_queries_by_branch_only(tmp_path, monkeypatch):
    # A branch pushed to origin with an OPEN PR (which may target a non-default
    # base like `staging` the server never learns) must advance the chip to
    # "pr" — the base-scoped lookup used to miss it and wedge on "pushed".
    wt = _init_repo(tmp_path / "r")  # default branch: main
    _git("checkout", "-q", "-b", "feat-b", cwd=str(wt))
    (wt / "b.txt").write_text("two\n")
    _git("add", "-A", cwd=str(wt))
    _git("commit", "-q", "-m", "work", cwd=str(wt))
    head = _git("rev-parse", "HEAD", cwd=str(wt))
    # Confirmed pushed: origin's tip for the branch matches the local head.
    monkeypatch.setattr(server, "_origin_branch_sha", lambda w, b: head)

    calls = []

    def fake_pr_info(*args, **kwargs):
        calls.append((args, kwargs))
        return {"url": "https://example.test/pr/7", "state": "OPEN"}

    monkeypatch.setattr(server, "_pr_info", fake_pr_info)

    inst = _FakeInst(str(wt), branch="feat-b", in_place=True)
    res = server._session_stage(inst)

    assert res["stage"] == "pr"
    assert res["pr_url"] == "https://example.test/pr/7"
    # Queried by (worktree, head branch) ONLY — no base positional/kw leaked in
    # (guards the removed 'base' arg from sliding into `force`).
    assert calls, "expected _pr_info to be consulted once commits exist"
    args, kwargs = calls[0]
    assert args == (str(wt), "feat-b")
    assert "base" not in kwargs


def test_pr_info_not_queried_without_commits_beyond_base(tmp_path, monkeypatch):
    # No commits beyond the fork-point -> the (bounded, cached) gh lookup must be
    # skipped entirely and the chip sits at "agent".
    wt = _init_repo(tmp_path / "r")
    _git("checkout", "-q", "-b", "feat-b", cwd=str(wt))  # no new commits

    called = []
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: called.append((a, k)))

    inst = _FakeInst(str(wt), branch="feat-b", in_place=True)
    res = server._session_stage(inst)

    assert res["stage"] == "agent"
    assert called == []


def test_inplace_degenerate_repo_falls_back_to_own_branch(tmp_path):
    # No origin/HEAD and no main/master anywhere: base must fall back to the
    # live branch (branch-is-its-own-base) instead of a nonexistent "main".
    wt = _init_repo(tmp_path / "r", branch="trunk")
    inst = _FakeInst(str(wt), base_branch="trunk", branch="trunk", in_place=True)
    assert server._session_base_branch(inst) == "trunk"


# --------------------------------------------------------------------------- #
# action endpoints follow the live branch
# --------------------------------------------------------------------------- #
def test_merge_pr_targets_live_branch_and_resets_flow(tmp_path, monkeypatch):
    wt = _init_repo(tmp_path / "r")
    _git("checkout", "-q", "-b", "feat-live", cwd=str(wt))
    status = _write_status(wt)
    inst = _FakeInst(str(wt), branch="feat-old", in_place=True)
    monkeypatch.setitem(server.ENGINE.instances, inst.Title, inst)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/" + name)
    server._ORIGIN_SHA_CACHE[(str(wt), "feat-live")] = (float("inf"), "sha")

    calls = []

    def fake_run_capped(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(server, "_run_capped", fake_run_capped)

    resp = asyncio.run(server.instance_merge_pr(inst.Title))

    assert resp.status_code == 200
    merge_cmds = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge_cmds and merge_cmds[0][3] == "feat-live"  # live, not stored
    assert any(c[:2] == ["git", "-C"] and "fetch" in c for c in calls)
    assert not status.exists()
    assert (str(wt), "feat-live") not in server._ORIGIN_SHA_CACHE


def test_push_branch_marks_live_branch_pending(tmp_path, monkeypatch):
    from backend.web.core import git_ops

    wt = _init_repo(tmp_path / "r")
    _git("checkout", "-q", "-b", "feat-live", cwd=str(wt))
    inst = _FakeInst(str(wt), branch="feat-old")
    monkeypatch.setitem(server.ENGINE.instances, inst.Title, inst)
    monkeypatch.setattr(server, "_has_origin", lambda wt, fresh=False: True)
    monkeypatch.setattr(
        server, "_ensure_shell_session", lambda title, wt: ("sess", None)
    )
    monkeypatch.setattr(server, "_send_to_shell", lambda name, cmd: None)
    git_ops._ORIGIN_SHA_PENDING.clear()

    resp = asyncio.run(server.instance_push_branch(inst.Title, {"force": True}))

    assert resp.status_code == 200
    # The shell push is fire-and-forget, so the branch isn't on origin yet: the
    # LIVE branch is marked pending (polls re-query origin until it lands), while
    # the stale stored inst.Branch is left untouched.
    assert (str(wt), "feat-live") in git_ops._ORIGIN_SHA_PENDING  # live marked
    assert (str(wt), "feat-old") not in git_ops._ORIGIN_SHA_PENDING
    git_ops._ORIGIN_SHA_PENDING.clear()
