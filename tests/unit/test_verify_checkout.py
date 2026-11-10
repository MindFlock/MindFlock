"""The server prepares the verify checker's checkout (test_plans_driver).

Real temp git repos only (``tmp_path``), with the inherited ``GIT_*`` repo
variables unset and the user's global/system git config shut out, so nothing
here can touch a real clone. The engine is a stand-in: these tests never read
``server.ENGINE``.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from backend.web.core import test_plans_driver as driver


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(cwd, msg: str) -> str:
    (cwd / "f.txt").write_text(msg + "\n")
    _git(cwd, "add", "f.txt")
    _git(cwd, "commit", "-q", "-m", msg)
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
    ):
        monkeypatch.delenv(var, raising=False)
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text("[user]\n\tname = t\n\temail = t@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gcfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    instances: dict = {}
    fake = SimpleNamespace(ENGINE=SimpleNamespace(instances=instances))
    monkeypatch.setattr(driver, "_server", lambda: fake)
    wts = tmp_path / "wts"
    monkeypatch.setattr(
        driver, "_verify_checkout_path", lambda title: str(wts / (title + "_checkout"))
    )
    return SimpleNamespace(tmp=tmp_path, instances=instances, wts=wts)


@pytest.fixture
def base(env):
    """A base clone with history and other sessions' worktrees hanging off it."""
    repo = env.tmp / "base"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "staging")
    first = _commit(repo, "one")
    second = _commit(repo, "two")
    _git(repo, "branch", "feature", first)
    # Unrelated worktrees, one on its own branch at the base's HEAD — the
    # "just-downloaded-ressie-git" shape whose HEAD the broken run recorded.
    _git(repo, "worktree", "add", "-q", str(env.tmp / "other"), "-b", "other")
    _git(repo, "worktree", "add", "-q", "--detach", str(env.tmp / "det"), first)
    return SimpleNamespace(path=repo, first=first, second=second)


def _inst(path):
    return SimpleNamespace(GetWorktreePath=lambda: str(path))


def test_base_with_worktrees_gets_detached_checkout_at_sha(env, base):
    out = driver._prepare_verify_checkout(
        str(base.path), "verify-p1-abc1234", base.first
    )
    assert out["ok"] is True, out
    path = out["path"]
    assert out["sha"] == base.first
    assert out["reused"] is False
    assert path == os.path.realpath(str(env.wts / "verify-p1-abc1234_checkout"))
    assert _git(path, "rev-parse", "--is-inside-work-tree") == "true"
    assert _git(path, "rev-parse", "HEAD") == base.first
    # Detached: no branch is checked out there.
    assert _git(path, "branch", "--show-current") == ""
    assert os.path.isfile(os.path.join(path, ".git"))  # a LINKED worktree
    # The base clone and the other sessions' trees are untouched.
    assert _git(base.path, "rev-parse", "HEAD") == base.second
    assert _git(base.path, "branch", "--show-current") == "staging"
    assert _git(env.tmp / "other", "rev-parse", "HEAD") == base.second


def test_shared_core_bare_true_is_worked_around_not_rewritten(env, base):
    _git(base.path, "config", "core.bare", "true")
    # The exact failure the agent hit in the base clone.
    probe = subprocess.run(
        ["git", "-C", str(base.path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() != "true"

    out = driver._prepare_verify_checkout(
        str(base.path), "verify-p2-abc1234", base.first
    )
    assert out["ok"] is True, out
    assert "core.bare=true" in out["warning"]
    assert _git(out["path"], "rev-parse", "--is-inside-work-tree") == "true"
    assert _git(out["path"], "rev-parse", "HEAD") == base.first
    # Never rewritten silently: the user's config still says what it said.
    assert _git(base.path, "config", "--bool", "core.bare") == "true"


def test_unknown_sha_is_checkout_failed(env, base):
    out = driver._prepare_verify_checkout(
        str(base.path), "verify-p3-deadbee", "deadbeef" * 5
    )
    assert out["ok"] is False
    assert out["reason"] == driver.CHECKOUT_FAILED == "checkout_failed"
    assert "deadbeef" in out["error"]
    assert not os.path.exists(env.wts / "verify-p3-deadbee_checkout")


def test_missing_repo_and_blank_ref_are_checkout_failed(env, base):
    gone = driver._prepare_verify_checkout(
        str(env.tmp / "nope"), "verify-p4-abc1234", base.first
    )
    assert gone["ok"] is False and gone["reason"] == "checkout_failed"
    blank = driver._prepare_verify_checkout(str(base.path), "verify-p4-abc1234", "")
    assert blank["ok"] is False and blank["reason"] == "checkout_failed"


def test_origin_branch_is_fetched_then_resolved(env):
    upstream = env.tmp / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "a")
    _git(upstream, "branch", "sandbox")
    clone = env.tmp / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(clone)],
        check=True,
        capture_output=True,
    )
    # Origin moves on after the clone's last fetch: a stale origin/sandbox.
    _git(upstream, "checkout", "-q", "sandbox")
    newer = _commit(upstream, "b")
    assert _git(clone, "rev-parse", "origin/sandbox") != newer

    out = driver._prepare_verify_checkout(
        str(clone), "verify-p5-abc1234", "origin/sandbox"
    )
    assert out["ok"] is True, out
    assert out["sha"] == newer
    assert _git(out["path"], "rev-parse", "HEAD") == newer


def test_origin_branch_fetch_failure_is_checkout_failed(env, base):
    # No origin remote at all: a stale (here: absent) ref is never used.
    out = driver._prepare_verify_checkout(
        str(base.path), "verify-p6-abc1234", "origin/main"
    )
    assert out["ok"] is False and out["reason"] == "checkout_failed"
    assert "fetch" in out["error"]


def test_open_session_in_its_checkout_is_re_detached(env, base):
    first = driver._prepare_verify_checkout(
        str(base.path), "verify-p7-abc1234", base.first
    )
    assert first["ok"], first
    # The agent wandered off the commit.
    _git(first["path"], "checkout", "-q", "--detach", base.second)
    env.instances["verify-p7-abc1234"] = _inst(first["path"])

    again = driver._prepare_verify_checkout(
        str(base.path), "verify-p7-abc1234", base.first
    )
    assert again["ok"] is True, again
    assert again["reused"] is True
    assert again["path"] == first["path"]
    assert _git(again["path"], "rev-parse", "HEAD") == base.first


def test_open_session_in_the_base_clone_is_refused(env, base):
    # The broken shape: an in-place checker IN the base clone.
    env.instances["verify-p8-abc1234"] = _inst(base.path)
    out = driver._prepare_verify_checkout(
        str(base.path), "verify-p8-abc1234", base.first
    )
    assert out["ok"] is False and out["reason"] == "checkout_failed"
    assert _git(base.path, "rev-parse", "HEAD") == base.second


def test_dead_runs_leftover_checkout_is_replaced(env, base):
    first = driver._prepare_verify_checkout(
        str(base.path), "verify-p9-abc1234", base.second
    )
    assert first["ok"], first
    (env.wts / "verify-p9-abc1234_checkout" / "junk.txt").write_text("x")
    # No session owns it any more; the next run wants another commit.
    out = driver._prepare_verify_checkout(
        str(base.path), "verify-p9-abc1234", base.first
    )
    assert out["ok"] is True, out
    assert _git(out["path"], "rev-parse", "HEAD") == base.first
    assert not os.path.exists(os.path.join(out["path"], "junk.txt"))


def test_path_owned_by_another_session_is_refused(env, base):
    path = env.wts / "verify-p10-abc1234_checkout"
    path.mkdir(parents=True)
    env.instances["someone-else"] = _inst(path)
    out = driver._prepare_verify_checkout(
        str(base.path), "verify-p10-abc1234", base.first
    )
    assert out["ok"] is False and "someone-else" in out["error"]
    assert path.is_dir()


def test_verify_session_in_a_clone_checkout_is_not_usable(env, base):
    env.instances["verify-p11-abc1234"] = _inst(base.path)
    assert driver._verify_session_usable("verify-p11-abc1234") is False
    prepared = driver._prepare_verify_checkout(
        str(base.path), "verify-p12-abc1234", base.first
    )
    env.instances["verify-p12-abc1234"] = _inst(prepared["path"])
    assert driver._verify_session_usable("verify-p12-abc1234") is True
    # A fix session's contract is different: left as it was.
    env.instances["fix-p11"] = _inst(base.path)
    assert driver._verify_session_usable("fix-p11") is True


def test_verify_checkout_ref_mirrors_the_run_prompt():
    plan = {"sha": "a" * 40, "tip_sha": "b" * 40, "live_branch": "sandbox"}
    assert driver._verify_checkout_ref(plan, live=False) == "b" * 40
    assert driver._verify_checkout_ref(plan, live=True) == "origin/sandbox"
    assert (
        driver._verify_checkout_ref(plan, live=False, checkout="origin/x") == "origin/x"
    )
    assert driver._verify_checkout_ref({"sha": "c" * 40}, live=False) == "c" * 40
    assert driver._verify_checkout_ref({}, live=False) == "origin/main"
