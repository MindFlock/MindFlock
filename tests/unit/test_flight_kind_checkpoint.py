"""The ``checkpoint`` stop (``backend.web.core.flight_kinds.checkpoint``,
docs/flights.md "Round 15"): record HEAD (+ a stash of uncommitted work)
under a kept-alive ref, rewind to it through the ``rewind`` op, and drop the
refs when the flight goes — against real tmp git repos."""

import asyncio
import subprocess
import time

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flight_kinds as K
from backend.web.core import flights as F
from backend.web.core.flight_kinds import checkpoint as C

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
    monkeypatch.setattr(D, "_announce", lambda before, after: None)
    yield


def git(cwd, *args) -> str:
    cp = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return cp.stdout.strip()


def commit(cwd, name, text):
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", "edit %s" % name)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "a.txt", "one\n")
    return r


@pytest.fixture
def session(monkeypatch):
    """The session's facts the kind reads: its worktree and its activity."""
    state = {"folder": "", "activity": "idle"}
    monkeypatch.setattr(D, "worktree_of", lambda f: state["folder"])
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: {
            "present": True,
            "folder": state["folder"],
            "activity": state["activity"],
        },
    )
    return state


def _plan(*kinds):
    steps = [
        {"kind": k, "config": {"command": "true"}} if k == "command" else {"kind": k}
        for k in kinds
    ]
    return F.adhoc_plan(steps, name="t")


def take(f, step):
    """Run the checkpoint and record its outcome as the driver would."""
    out = asyncio.run(K.get("checkpoint").run(f, step, {"action": "run"}, NOW))
    assert out["action"] == "done"

    def fn(fl):
        F.op_jump(fl, step["id"])
        F.apply(fl, out, NOW)

    return F.mutate(f["id"], fn), out


def test_checkpoint_is_registered_as_a_ship_stop():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "checkpoint")
    assert row["family"] == "ship" and row["risk"] == [] and row["actor"] == "robot"
    assert [x["name"] for x in row["fields"]] == ["note"]
    step = F.normalize_step({"kind": "checkpoint"}, 0)
    assert step["config"] == {"note": ""} and step["title"] == "Checkpoint"
    step = F.normalize_step({"kind": "checkpoint", "config": {"note": "pre-split"}}, 0)
    assert step["title"] == "Checkpoint: pre-split"


def test_clean_worktree_records_head(repo, session):
    session["folder"] = str(repo)
    f = F.take_off(_plan("checkpoint"), session="s", now=NOW)
    step = f["steps"][0]
    f, out = take(f, step)
    d = out["detail"]
    head = git(repo, "rev-parse", "HEAD")
    assert d["sha"] == head and d["stash"] == ""
    assert d["receipt"] == "at %s" % head[:7]
    assert d["ref"] == "refs/mindflock/flights/%s/%s" % (f["id"], step["id"])
    assert git(repo, "rev-parse", d["ref"]) == head
    assert f["step_states"][step["id"]]["state"] == "done"
    assert f["state"] == "done", "the last stop: the flight lands"


def test_dirty_worktree_is_stashed_without_touching_it(repo, session):
    session["folder"] = str(repo)
    (repo / "a.txt").write_text("work in progress\n")
    f = F.take_off(_plan("checkpoint"), session="s", now=NOW)
    out = asyncio.run(K.get("checkpoint").run(f, f["steps"][0], {}, NOW))
    d = out["detail"]
    assert d["stash"] and d["stash"] != d["sha"]
    assert git(repo, "rev-parse", d["ref"]) == d["stash"]
    assert git(repo, "rev-parse", d["stash"] + "^1") == d["sha"]
    assert (repo / "a.txt").read_text() == "work in progress\n"
    assert git(repo, "stash", "list") == "", "never on the shared stash stack"


def test_no_worktree_fails_the_step(session):
    f = F.take_off(_plan("checkpoint"), session="s", now=NOW)
    with pytest.raises(K.StepFailed):
        asyncio.run(K.get("checkpoint").run(f, f["steps"][0], {}, NOW))


def test_judge_after_a_restart(repo, session):
    f = F.take_off(_plan("checkpoint"), session="s", now=NOW)
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    ss["state"] = "running"
    assert F.next_action(f, {"session": {"present": True}}, NOW)["action"] == "run"
    ss["detail"] = {"sha": "a" * 40, "receipt": "at aaaaaaa"}
    out = F.next_action(f, {"session": {"present": True}}, NOW)
    assert out["action"] == "done"


def test_rewind_resets_reapplies_and_flies_on(repo, session):
    session["folder"] = str(repo)
    (repo / "a.txt").write_text("uncommitted\n")
    f = F.take_off(_plan("checkpoint", "command", "command"), session="s", now=NOW)
    cp, nxt = f["steps"][0], f["steps"][1]
    f, out = take(f, cp)
    sha = out["detail"]["sha"]

    # The flight flies on; the agent commits and makes a mess.
    F.mutate(f["id"], lambda fl: F.op_jump(fl, f["steps"][2]["id"]))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "later")
    commit(repo, "b.txt", "b\n")
    (repo / "a.txt").write_text("mess\n")

    res = D.step_op(f["id"], cp["id"], "rewind", {})
    assert res["sha"] == sha
    assert git(repo, "rev-parse", "HEAD") == sha
    assert (repo / "a.txt").read_text() == "uncommitted\n"
    assert not (repo / "b.txt").exists()
    fresh = F.get(f["id"])
    assert res["flight"]["cursor"] == fresh["cursor"] == 1
    assert fresh["step_states"][nxt["id"]]["state"] == "pending"
    assert fresh["step_states"][cp["id"]]["state"] == "done"
    assert fresh["history"][-1]["event"] == "jumped"


def test_rewind_refuses_while_the_agent_works(repo, session):
    session["folder"] = str(repo)
    f = F.take_off(_plan("checkpoint", "command"), session="s", now=NOW)
    f, _ = take(f, f["steps"][0])
    commit(repo, "b.txt", "b\n")
    head = git(repo, "rev-parse", "HEAD")
    session["activity"] = "working"
    with pytest.raises(F.FlightError) as err:
        D.step_op(f["id"], f["steps"][0]["id"], "rewind", {})
    assert err.value.error == "working" and err.value.status == 409
    assert git(repo, "rev-parse", "HEAD") == head, "nothing was reset"


def test_rewind_refuses_without_a_checkpoint(repo, session):
    session["folder"] = str(repo)
    f = F.take_off(_plan("checkpoint", "command"), session="s", now=NOW)
    with pytest.raises(F.FlightError) as err:
        D.step_op(f["id"], f["steps"][0]["id"], "rewind", {})
    assert err.value.error == "no_checkpoint" and err.value.status == 409


def test_rewind_at_the_last_stop_leaves_the_cursor(repo, session):
    session["folder"] = str(repo)
    f = F.take_off(_plan("command", "checkpoint"), session="s", now=NOW)
    cp = f["steps"][1]
    out = asyncio.run(K.get("checkpoint").run(f, cp, {}, NOW))

    def fn(fl):
        F.op_jump(fl, cp["id"])
        fl["step_states"][cp["id"]].update(state="running", detail=out["detail"])

    f = F.mutate(f["id"], fn)
    commit(repo, "b.txt", "b\n")
    res = D.step_op(f["id"], cp["id"], "rewind", {})
    assert git(repo, "rev-parse", "HEAD") == out["detail"]["sha"]
    assert res["flight"]["cursor"] == 1
    assert [h["event"] for h in res["flight"]["history"]].count("jumped") == 1


def test_forget_drops_the_refs_even_with_the_worktree_gone(repo, session, tmp_path):
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    session["folder"] = str(wt)
    f = F.take_off(_plan("checkpoint", "command"), session="s", now=NOW)
    f, out = take(f, f["steps"][0])
    ref = out["detail"]["ref"]
    assert git(repo, "rev-parse", ref) == out["detail"]["sha"], "shared by the repo"
    # A stray ref of this flight (a checkpoint re-taken under another id).
    git(repo, "update-ref", "refs/mindflock/flights/%s/old" % f["id"], "HEAD")
    git(repo, "update-ref", "refs/mindflock/flights/other/x", "HEAD")
    git(repo, "worktree", "remove", "--force", str(wt))
    assert C.forget(f) == 2
    refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/mindflock/")
    assert refs == "refs/mindflock/flights/other/x"
    assert C.forget(f) == 0 and C.forget(None) == 0
