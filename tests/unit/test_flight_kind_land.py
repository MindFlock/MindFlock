"""The ``land`` stop (``flight_kinds/land.py``, docs/flights.md "Round 15"):
always the last stop, done only once the branch is on the default branch,
and the tidy-up (remote branch, session) after the flight has finished —
against a scratch git origin and fakes, never the real engine."""

import asyncio
import subprocess
import time
from types import SimpleNamespace

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import github_pr
from backend.web.core import session_end
from backend.web.core.flight_kinds import StepFailed
from backend.web.core.flight_kinds import land as L

NOW = time.time()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(github_pr, "find_pr_sync", lambda wt, branch: None)
    yield


def _git(cwd, *args):
    cp = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return cp.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """An origin (bare, default main) and a clone with branch ``feat`` one
    commit ahead, checked out in a worktree like a session's."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git(clone, "config", k, v)
    (clone / "a.txt").write_text("a\n")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-qm", "a")
    _git(clone, "push", "-q", "origin", "main")
    _git(clone, "remote", "set-head", "origin", "main")
    wt = tmp_path / "wt"
    _git(clone, "worktree", "add", "-q", "-b", "feat", str(wt))
    (wt / "b.txt").write_text("b\n")
    _git(wt, "add", "b.txt")
    _git(wt, "commit", "-qm", "b")
    _git(wt, "push", "-q", "origin", "feat")
    return SimpleNamespace(origin=origin, clone=clone, wt=wt)


def _merge(repo):
    _git(repo.clone, "merge", "-q", "--ff-only", "feat")
    _git(repo.clone, "push", "-q", "origin", "main")


@pytest.fixture
def srv(monkeypatch, repo):
    inst = SimpleNamespace(InPlace=False, Title="s")
    ns = SimpleNamespace(ENGINE=SimpleNamespace(instances={"s": inst}))
    monkeypatch.setattr(D, "_server", lambda: ns)
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: {
            "present": flight["session"] == "s",
            "folder": str(repo.wt),
            "branch": "feat",
        },
    )
    return ns


@pytest.fixture
def started(monkeypatch):
    got = []
    monkeypatch.setattr(L, "_start_close", lambda fid, sid, plan: got.append(plan))
    return got


def _flight(*steps):
    plan = F.adhoc_plan(list(steps) or [{"kind": "land"}], name="t")
    return F.take_off(plan, session="s", now=NOW)


def _run(f):
    step = f["steps"][-1]
    return asyncio.run(L._run(f, step, {"action": "run", "note": ""}, NOW))


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #
def test_land_is_a_ship_stop_that_cannot_be_undone():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "land")
    assert row["family"] == "ship" and row["risk"] == ["irreversible"]
    assert "land" in F.SESSION_KINDS
    step = F.normalize_step({"kind": "land"}, 0)
    assert step["config"] == {
        "delete_branch": True,
        "remote_branch": True,
        "close": "delete",
    }
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "land", "config": {"close": "burn"}}, 0)
    assert err.value.field == "steps[0].config.close"


def test_land_must_be_the_last_stop():
    steps = [{"kind": "land"}, {"kind": "notify", "config": {"text": "x"}}]
    with pytest.raises(F.PlanError) as err:
        F.normalize_plan({"name": "x", "steps": steps})
    assert str(err.value) == "a Finish stop is the last stop"
    assert err.value.field == "steps[0].kind"
    assert F.normalize_plan({"name": "x", "steps": steps[::-1]})["steps"][-1][
        "kind"
    ] == ("land")
    lenient = F.normalize_plan({"name": "x", "steps": steps}, strict=False)
    assert [s["kind"] for s in lenient["steps"]] == ["land", "notify"]


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def test_not_merged_fails(srv, repo, started):
    with pytest.raises(StepFailed) as err:
        _run(_flight())
    assert str(err.value) == "not merged into main"
    assert started == []


def test_merged_lands_and_closes_after(srv, repo, started):
    _merge(repo)
    out = _run(_flight())
    assert out["action"] == "done"
    assert out["note"] == "finished on main"
    assert out["detail"]["receipt"] == "finished"
    plan = out["detail"]["pending_close"]
    assert plan["mode"] == "delete" and plan["branch"] == "feat"
    assert plan["repo"] == str(repo.clone)
    assert started == [plan]


def test_a_merged_pr_counts_after_a_squash(srv, repo, started, monkeypatch):
    monkeypatch.setattr(
        github_pr,
        "find_pr_sync",
        lambda wt, branch: {"state": "MERGED", "base": "main", "number": 7},
    )
    f = _flight()
    assert _run(f)["action"] == "done"
    monkeypatch.setattr(
        github_pr,
        "find_pr_sync",
        lambda wt, branch: {"state": "MERGED", "base": "release", "number": 7},
    )
    with pytest.raises(StepFailed):
        _run(f)


def test_uncommitted_files_close_instead_of_delete(srv, repo, started):
    _merge(repo)
    (repo.wt / "scratch.txt").write_text("x\n")
    out = _run(_flight())
    assert out["detail"]["pending_close"]["mode"] == "close"
    assert (
        out["note"]
        == "finished on main — closing, not deleting: it has uncommitted files"
    )


def test_in_place_closes_instead_of_delete(srv, repo, started):
    _merge(repo)
    srv.ENGINE.instances["s"].InPlace = True
    out = _run(_flight())
    assert out["detail"]["pending_close"]["mode"] == "close"
    assert "runs in place" in out["note"]


def test_not_the_last_step_refuses_at_run(srv, repo, started):
    _merge(repo)
    plan = F.adhoc_plan([{"kind": "notify", "config": {"text": "x"}}], name="t")
    plan["steps"].insert(0, F.normalize_step({"kind": "land", "id": "s9"}, 0))
    f = F.take_off(plan, session="s", now=NOW)
    with pytest.raises(StepFailed) as err:
        asyncio.run(L._run(f, f["steps"][0], {"action": "run"}, NOW))
    assert str(err.value) == "a Finish stop is the last stop"


def test_a_restart_mid_run_runs_again():
    f = _flight()
    step = f["steps"][0]
    out = L._judge(f, step, f["step_states"][step["id"]], {}, NOW)
    assert out["action"] == "run"


def test_driver_finishes_the_flight_done(srv, repo, started):
    _merge(repo)
    f = _flight()
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    F.apply(f, out, NOW)
    assert f["state"] == "done"
    assert f["step_states"][f["steps"][0]["id"]]["detail"]["receipt"] == "finished"


def test_driver_turns_not_merged_into_a_halt(srv, repo, started):
    f = _flight()
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["action"] == "fail" and out["reason"] == "not merged into main"
    assert out["then"] == "halt"


# --------------------------------------------------------------------------- #
# The tidy-up thread
# --------------------------------------------------------------------------- #
@pytest.fixture
def ended(monkeypatch, repo):
    """A fake ``end_session``: a wipe removes the worktree (and the branch when
    ``end.drops_branch``, as ``Kill`` does for a branch the session made)."""
    got = []

    def end(engine, title, *, wipe, **kw):
        got.append((title, wipe))
        if wipe and got.count((title, True)) == 1 and getattr(end, "in_use", False):
            raise session_end.FolderInUse(["s-copy"])
        if wipe:
            _git(repo.clone, "worktree", "remove", "--force", str(repo.wt))
            if getattr(end, "drops_branch", False):
                _git(repo.clone, "branch", "-D", "feat")

    monkeypatch.setattr(session_end, "end_session", end)
    return SimpleNamespace(calls=got, fn=end)


def _landed(srv, repo, started, **config):
    _merge(repo)
    f = _flight({"kind": "land", "config": config})
    out = _run(f)
    sid = f["steps"][0]["id"]
    F.mutate(f["id"], lambda x: F.apply(x, out, NOW))
    return f["id"], sid, out["detail"]["pending_close"]


def _remote_has(repo, branch):
    return bool(_git(repo.origin, "branch", "--list", branch))


def test_close_later_deletes_the_remote_branch_and_the_session(
    srv, repo, started, ended
):
    fid, sid, plan = _landed(srv, repo, started)
    assert _remote_has(repo, "feat")
    out = L._close_later(fid, sid, plan, sleep=lambda s: None)
    assert out == "deleted"
    assert ended.calls == [("s", True)]
    assert not _remote_has(repo, "feat")
    # The fake end_session kept the branch (one that existed before the
    # session), so the land deletes it.
    assert not _git(repo.clone, "branch", "--list", "feat")
    detail = F.get(fid)["step_states"][sid]["detail"]
    assert detail["closed"] == "deleted" and "pending_close" not in detail


def test_close_later_keeps_what_the_config_keeps(srv, repo, started, ended):
    fid, sid, plan = _landed(
        srv, repo, started, remote_branch=False, delete_branch=False, close="close"
    )
    assert L._close_later(fid, sid, plan, sleep=lambda s: None) == "closed"
    assert ended.calls == [("s", False)]
    assert _remote_has(repo, "feat")
    assert _git(repo.clone, "branch", "--list", "feat")


def test_delete_keeps_the_branch_when_told(srv, repo, started, ended):
    ended.fn.drops_branch = True
    sha = _git(repo.wt, "rev-parse", "HEAD")
    fid, sid, plan = _landed(srv, repo, started, delete_branch=False)
    assert L._close_later(fid, sid, plan, sleep=lambda s: None) == "deleted"
    assert not _remote_has(repo, "feat")
    assert _git(repo.clone, "rev-parse", "refs/heads/feat") == sha


def test_close_later_falls_back_to_close_when_the_folder_is_shared(
    srv, repo, started, ended
):
    ended.fn.in_use = True
    fid, sid, plan = _landed(srv, repo, started, remote_branch=False)
    assert L._close_later(fid, sid, plan, sleep=lambda s: None) == "closed"
    assert ended.calls == [("s", True), ("s", False)]


def test_close_later_leaves_a_flight_that_did_not_land(srv, repo, started, ended):
    _merge(repo)
    f = _flight()
    plan = _run(f)["detail"]["pending_close"]
    F.mutate(f["id"], lambda x: F.op_cancel(x, "changed my mind"))
    assert (
        L._close_later(f["id"], f["steps"][0]["id"], plan, sleep=lambda s: None) == ""
    )
    assert ended.calls == []
    assert _remote_has(repo, "feat")
