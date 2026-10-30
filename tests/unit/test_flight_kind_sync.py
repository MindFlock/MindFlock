"""The ``sync`` stop (``backend.web.core.flight_kinds.sync``, docs/flights.md
"Round 15"): rebase / merge the session's branch onto another, against real
tmp git repos — a clean rebase, nothing to do, a merge, a conflict left for
the agent and re-checked on retry, a restart mid-job, a dirty worktree."""

import asyncio
import subprocess
import time

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flight_kinds as K
from backend.web.core import flights as F
from backend.web.core.flight_kinds import sync as S

NOW = time.time()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv("GIT_%s_NAME" % who, "Test")
        monkeypatch.setenv("GIT_%s_EMAIL" % who, "test@example.com")
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "wake", lambda: None)
    yield


def git(cwd, *args) -> str:
    cp = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return cp.stdout.strip()


def commit(cwd, name, text, msg=None):
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", msg or "edit %s" % name)


@pytest.fixture
def repos(tmp_path):
    """``up`` (the remote, on main) and ``work`` (its clone, on ``feat`` with
    two commits of its own)."""
    up = tmp_path / "up"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    commit(up, "a.txt", "one\n")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(up), str(work))
    git(work, "checkout", "-q", "-b", "feat")
    commit(work, "b.txt", "b\n")
    commit(work, "c.txt", "c\n")
    return up, work


@pytest.fixture
def flight(monkeypatch):
    def make(folder, **config):
        monkeypatch.setattr(D, "worktree_of", lambda f: str(folder))
        plan = F.adhoc_plan([{"kind": "sync", "config": config}], name="t")
        f = F.take_off(plan, session="s", now=NOW)
        return f

    return make


def _wait_job(f, step):
    for _ in range(300):
        job = D.job_of(f, step)
        if job is None or not job.get("running"):
            return job
        time.sleep(0.02)
    raise AssertionError("the sync job never ended")


def fly(f):
    """run → (job ends) → observe → judge, as the driver would; the run's
    detail is merged into the step state the way ``_record_outcome`` does."""
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    ss["state"] = "running"
    out = asyncio.run(K.get("sync").run(f, step, {"action": "run", "note": ""}, NOW))
    ss["detail"].update(out.get("detail") or {})
    _wait_job(f, step)
    return judge(f)


def judge(f):
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    seen = asyncio.run(K.get("sync").observe(f, step, ss, {}, NOW))
    return F.next_action(f, {"session": {"present": True}, "sync": seen}, NOW)


def test_sync_is_registered_as_a_ship_stop():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "sync")
    assert row["family"] == "ship" and row["risk"] == [] and row["actor"] == "robot"
    assert [x["name"] for x in row["fields"]] == ["with", "how", "fetch"]
    assert "sync" in F.SESSION_KINDS


def test_config_defaults_and_refusals():
    step = F.normalize_step({"kind": "sync"}, 0)
    assert step["config"] == {"with": "", "how": "rebase", "fetch": True}
    assert step["title"] == "Sync with main"
    step = F.normalize_step(
        {"kind": "sync", "config": {"with": "dev", "how": "MERGE", "fetch": "no"}}, 0
    )
    assert step["config"] == {"with": "dev", "how": "merge", "fetch": False}
    assert step["title"] == "Merge dev"
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "sync", "config": {"how": "squash"}}, 0)
    assert err.value.field == "steps[0].config.how"
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "sync", "config": {"with": "--force; rm"}}, 0)
    assert err.value.field == "steps[0].config.with"
    lenient = F._step_config("sync", {"with": "-x", "how": "?"}, False, "s")
    assert lenient == {"with": "", "how": "rebase", "fetch": True}


def test_default_branch_follows_origin_head(repos):
    up, work = repos
    assert S.default_branch(str(work)) == "main"
    git(up, "branch", "-m", "main", "trunk")
    git(work, "fetch", "-q", "origin")
    git(work, "remote", "set-head", "origin", "trunk")
    assert S.default_branch(str(work)) == "trunk"


def test_rebase_onto_a_moved_main(repos, flight):
    up, work = repos
    commit(up, "a.txt", "two\n")
    f = flight(work)
    out = fly(f)
    assert out["action"] == "done", out
    assert out["detail"]["receipt"] == "rebased 2 commits"
    tip = git(work, "rev-parse", "origin/main")
    assert git(work, "merge-base", "origin/main", "HEAD") == tip
    assert (work / "a.txt").read_text() == "two\n"


def test_nothing_to_do_is_up_to_date(repos, flight):
    _, work = repos
    out = fly(flight(work))
    assert out["action"] == "done"
    assert out["detail"]["receipt"] == "up to date with main"


def test_merge_receipt(repos, flight):
    up, work = repos
    commit(up, "d.txt", "d\n")
    out = fly(flight(work, how="merge"))
    assert out["action"] == "done", out
    assert out["detail"]["receipt"] == "merged main"
    assert len(git(work, "log", "-1", "--format=%P").split()) == 2


def test_fetch_off_syncs_with_what_is_known(repos, flight):
    up, work = repos
    commit(up, "a.txt", "two\n")
    out = fly(flight(work, fetch=False))
    assert out["action"] == "done"
    assert out["detail"]["receipt"] == "up to date with main", "origin/main not fetched"


def test_without_origin_the_local_branch_is_the_target(tmp_path, flight):
    repo = tmp_path / "solo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "a.txt", "one\n")
    git(repo, "checkout", "-q", "-b", "feat")
    commit(repo, "b.txt", "b\n")
    git(repo, "checkout", "-q", "main")
    commit(repo, "a.txt", "two\n")
    git(repo, "checkout", "-q", "feat")
    f = flight(repo)
    out = fly(f)
    assert out["action"] == "done", out
    assert f["step_states"][f["steps"][0]["id"]]["detail"]["ref"] == "main"
    assert out["detail"]["receipt"] == "rebased 1 commit"


def test_dirty_worktree_is_refused(repos, flight):
    _, work = repos
    (work / "new.txt").write_text("untracked counts\n")
    f = flight(work)
    with pytest.raises(K.StepFailed) as err:
        asyncio.run(
            K.get("sync").run(f, f["steps"][0], {"action": "run", "note": ""}, NOW)
        )
    assert "commit them first" in str(err.value)
    assert D.job_of(f, f["steps"][0]) is None


def test_dirty_refusal_through_the_driver_fails_the_step(repos, flight):
    _, work = repos
    (work / "b.txt").write_text("changed\n")
    f = flight(work)
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["action"] == "fail" and "uncommitted" in out["reason"]


def test_conflict_is_left_for_the_agent_and_rechecked_on_retry(repos, flight):
    up, work = repos
    commit(up, "b.txt", "theirs\n")
    f = flight(work)
    out = fly(f)
    assert out["action"] == "fail"
    assert out["detail"]["conflicts"] == ["b.txt"]
    assert "git rebase --continue" in out["reason"] and "b.txt" in out["reason"]
    assert S.in_progress(str(work)) == "rebase", "a conflict is never aborted"

    # The retry (the core runs `run` again) re-checks, never starts over.
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    ss["detail"] = {}
    again = asyncio.run(K.get("sync").run(f, step, {"action": "run", "note": ""}, NOW))
    assert again["detail"]["resumed"] == "rebase"
    ss["detail"].update(again["detail"])
    assert judge(f)["action"] == "fail"

    # The agent resolves it.
    (work / "b.txt").write_text("resolved\n")
    git(work, "add", "b.txt")
    subprocess.run(
        ["git", "-c", "core.editor=true", "rebase", "--continue"],
        cwd=work,
        check=True,
        capture_output=True,
    )
    out = judge(f)
    assert out["action"] == "done", out
    assert out["detail"]["receipt"] == "rebased 2 commits"


def test_merge_conflict_asks_for_a_commit(repos, flight):
    up, work = repos
    commit(up, "b.txt", "theirs\n")
    out = fly(flight(work, how="merge"))
    assert out["action"] == "fail"
    assert "git commit" in out["reason"] and out["detail"]["conflicts"] == ["b.txt"]
    assert S.in_progress(str(work)) == "merge"


def test_interrupted_job_is_rechecked_not_rerun_blindly(repos, flight):
    up, work = repos
    commit(up, "a.txt", "two\n")
    f = flight(work)
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    ss["state"] = "running"
    ss["detail"] = {
        "folder": str(work),
        "target": "main",
        "ref": "origin/main",
        "how": "rebase",
        "before": git(work, "rev-parse", "HEAD"),
    }
    git(work, "fetch", "-q", "origin")
    # No job this server run and not synced yet: sync again.
    assert judge(f)["action"] == "run"
    # It finished before the restart: done without running anything.
    git(work, "rebase", "-q", "origin/main")
    out = judge(f)
    assert out["action"] == "done" and out["detail"]["receipt"] == "rebased 2 commits"
    assert D.job_of(f, step) is None


def test_a_failing_command_fails_with_its_output(repos, flight):
    _, work = repos
    out = fly(flight(work, **{"with": "nosuch"}))
    assert out["action"] == "fail"
    assert "nosuch" in out["reason"]
