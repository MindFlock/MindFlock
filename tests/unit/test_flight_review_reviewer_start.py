"""The review stop's reviewer, as a real route met it (a PR from Intake,
provisioned as a clone of ANOTHER repository than the one MindFlock runs in):

1. a flight-spawned worker of a provisioned session works on the session's
   own repository, forked from its HEAD — not on the server's folder (its
   ``Path``), not on a provisioned twin forked from the base branch;
2. a retry never reuses a title whose provisioned leftover (the closed
   reviewer's ``<workspace_dir>/mindflock-<title>`` clone or
   ``mindflock/<title>`` branch) makes the create fail in the background;
3. a reviewer that vanished without ever starting is "couldn't start the
   reviewer: <why>" — one fresh reviewer automatically, then a halt for the
   person — never "the reviewer did not finish" handed to the agent.

Real git repos in ``tmp_path``; a fake server namespace (never the live
engine)."""

import asyncio
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import launch_gate as LG
from backend.web.core.flight_kinds import review as R

NOW = time.time()
CFG = {"model": "", "program": "", "fence_only": "", "report_back": True}


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    for k, v in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(D, "_row", lambda title: {})
    monkeypatch.setattr(LG, "waiting", lambda t: None)
    monkeypatch.setattr(LG, "last_outcome", lambda t: None)
    R._FINISHED.clear()
    yield
    R._FINISHED.clear()


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def repo(path, name="readme.md"):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / name).write_text("hello\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


class Inst:
    def __init__(self, folder, path, strategy="clone", branch="feature"):
        self.Provisioned = True
        self.WorkspaceStrategy = strategy
        self.Path = str(path)
        self.Branch = branch
        self.BaseBranch = "main"
        self.Program = "claude"
        self._folder = str(folder)

    def GetWorktreePath(self):
        return self._folder


def fake_server(monkeypatch, instances, failures=None):
    ns = SimpleNamespace(
        ENGINE=SimpleNamespace(instances=instances),
        _pending_rows=lambda: [],
        _CREATE_FAILURES=failures if failures is not None else {},
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    return ns


@pytest.fixture
def pr_clone(tmp_path, monkeypatch):
    """``pr-x``: a provisioned CLONE of sitecheck at ``<ws>/pr-x`` on branch
    ``feature`` (one commit ahead of main), whose ``Path`` is the server's own
    folder — another repository (MindFlock's)."""
    app = repo(tmp_path / "app", "mindflock.py")
    upstream = repo(tmp_path / "sitecheck", "bot.py")
    ws = tmp_path / "workspaces"
    ws.mkdir()
    clone = ws / "pr-x"
    git(tmp_path, "clone", "-q", str(upstream), str(clone))
    git(clone, "checkout", "-q", "-b", "feature")
    (clone / "fix.py").write_text("x = 1\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "the change")
    inst = Inst(clone, app)
    ns = fake_server(monkeypatch, {"pr-x": inst})
    return SimpleNamespace(app=app, clone=clone, ws=ws, inst=inst, srv=ns)


# --------------------------------------------------------------------------- #
# 1. The reviewer works on the session's repository, at its change
# --------------------------------------------------------------------------- #
def test_a_provisioned_clone_parents_worker_forks_from_its_own_checkout(pr_clone):
    payload, _provider, _prefix = D._split_base({"session": "pr-x"}, {}, CFG)
    root = payload["repo_path"]
    assert os.path.realpath(root) == os.path.realpath(pr_clone.clone)
    assert os.path.realpath(root) != os.path.realpath(pr_clone.app)
    assert "provisioned" not in payload and "workspace_strategy" not in payload
    assert payload["base_ref"] == git(pr_clone.clone, "rev-parse", "HEAD")
    assert payload["base_branch"] == "feature"
    assert payload["parent"] == "pr-x" and payload["spawned"] is True
    # What the reviewer is asked to run works where it is forked from: the
    # session's change, against the session's base.
    diff = git(root, "diff", "--name-only", "main...%s" % payload["base_ref"])
    assert diff.splitlines() == ["fix.py"]


def test_a_provisioned_worktree_parents_worker_forks_from_the_base_clone(
    tmp_path, monkeypatch
):
    app = repo(tmp_path / "app", "mindflock.py")
    base = repo(tmp_path / "ws" / "_base_sitecheck", "bot.py")
    wt = tmp_path / "ws" / "pr-y"
    git(base, "worktree", "add", "-q", "-b", "mindflock/pr-y", str(wt))
    (wt / "fix.py").write_text("y = 2\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "the change")
    fake_server(
        monkeypatch, {"pr-y": Inst(wt, app, "worktree", branch="mindflock/pr-y")}
    )
    payload, _p, _x = D._split_base({"session": "pr-y"}, {}, CFG)
    assert os.path.realpath(payload["repo_path"]) == os.path.realpath(base)
    assert payload["base_ref"] == git(wt, "rev-parse", "HEAD")
    assert payload["base_branch"] == "mindflock/pr-y"
    assert "provisioned" not in payload


def test_a_provisioned_parent_without_a_readable_checkout_is_refused(
    tmp_path, monkeypatch
):
    app = repo(tmp_path / "app")
    fake_server(monkeypatch, {"pr-z": Inst(tmp_path / "missing", app)})
    with pytest.raises(D._StepFailed) as err:
        D._split_base({"session": "pr-z"}, {}, CFG)
    assert "couldn't read pr-z's checkout" in str(err.value)


# --------------------------------------------------------------------------- #
# 2. A retry never reuses a title a leftover makes fail
# --------------------------------------------------------------------------- #
def _title(parent, repo_path="", branch_prefix=None, tried=None):
    probe = D._taken_by(D._leftover_probe(parent), tried or set())
    return D._free_worker_title("review-", repo_path, branch_prefix, probe)


def test_a_closed_provisioned_reviewers_clone_keeps_its_title(pr_clone):
    assert _title("pr-x") == "review-1"
    # The first reviewer was closed, kept: its clone stays in the workspace.
    (pr_clone.ws / "mindflock-review-1").mkdir()
    assert _title("pr-x") == "review-2"


def test_a_closed_provisioned_reviewers_branch_keeps_its_title(tmp_path, monkeypatch):
    app = repo(tmp_path / "app")
    base = repo(tmp_path / "ws" / "_base_sitecheck")
    wt = tmp_path / "ws" / "pr-y"
    git(base, "worktree", "add", "-q", "-b", "mindflock/pr-y", str(wt))
    fake_server(
        monkeypatch, {"pr-y": Inst(wt, app, "worktree", branch="mindflock/pr-y")}
    )
    assert _title("pr-y") == "review-1"
    git(base, "branch", "mindflock/review-1")
    assert _title("pr-y") == "review-2"


def test_the_servers_own_branch_prefix_is_checked(pr_clone, monkeypatch):
    # A plain worktree worker's branch is <the server's prefix><title>, which
    # a provisioned session's row (branch "feature") does not show.
    pr_clone.srv._session_branch_name = lambda t: "me/" + t
    git(pr_clone.clone, "branch", "me/review-1")
    assert _title("pr-x", str(pr_clone.clone)) == "review-2"


def test_a_title_whose_create_just_failed_is_not_reused(pr_clone):
    pr_clone.srv._CREATE_FAILURES["review-1"] = {"error": "boom", "ts": time.time()}
    assert _title("pr-x") == "review-2"
    # A refusal this spawn already met is skipped too.
    assert _title("pr-x", tried={"review-2"}) == "review-3"


def test_spawn_worker_moves_past_a_title_the_create_refuses(pr_clone, monkeypatch):
    from backend.web.core import lineage as _lineage
    from backend.web.core import session_create as _sc

    monkeypatch.setattr(D, "_footer_ok", lambda provider: False)
    monkeypatch.setattr(_lineage, "spawn_limit_error", lambda *a: None)
    (pr_clone.ws / "mindflock-review-1").mkdir()
    seen: list = []

    async def create(payload, auto=""):
        seen.append(dict(payload))
        if payload["title"] == "review-2":
            return 409, {"error": "a branch named me/review-2 already exists"}
        return 202, {"title": payload["title"]}

    monkeypatch.setattr(_sc, "create_result", create)
    flight = {"session": "pr-x"}
    title = asyncio.run(
        D.spawn_worker(flight, {"config": {}}, "go", title_prefix="review")
    )
    assert [p["title"] for p in seen] == ["review-2", "review-3"]
    assert title == "review-3"
    assert os.path.realpath(seen[-1]["repo_path"]) == os.path.realpath(pr_clone.clone)
    assert "provisioned" not in seen[-1]


# --------------------------------------------------------------------------- #
# 3. A reviewer that never started is not "did not finish" for the agent
# --------------------------------------------------------------------------- #
def sess():
    return {"present": True, "status": "running", "activity": "idle"}


def started(monkeypatch, failures):
    """A route on session ``s`` whose review stop (on_fail: agent) spawned
    ``review-1`` at NOW; ``review-1`` is gone (its background create failed)."""
    fake_server(monkeypatch, {"s": SimpleNamespace(BaseBranch="main")}, failures)
    plan = F.save_plan(
        {"name": "P", "steps": [{"kind": "review", "on_fail": "agent"}]}, now=NOW
    )
    f = F.take_off(plan, session="s", now=NOW)
    a = F.next_action(f, {"session": sess()}, NOW)
    F.apply(f, dict(a, detail={"children": ["review-1"], "base": "main"}), NOW)
    return f


def gone(prev=None):
    """The review observation of a reviewer that is gone without a report."""
    entry = R._entry({"title": "review-1", "present": False}, None, prev, NOW, NOW)
    return {"entry": entry, "text": "", "report": False}


def verdict(f, review, now):
    ss = f["step_states"][f["steps"][0]["id"]]
    obs = {"session": sess(), "review": review, "launch": D._launch_obs(ss)}
    return D._launch_verdict(f, obs, F.next_action(f, obs, now), now)


def test_a_reviewer_whose_create_failed_is_asked_again_then_halts(monkeypatch):
    failures = {"review-1": {"error": "a workspace already exists", "ts": NOW + 1}}
    f = started(monkeypatch, failures)
    sid = f["steps"][0]["id"]
    # Even the judge alone never hands it to the agent: it asks again.
    raw = F.next_action(f, {"session": sess(), "review": gone()}, NOW + 5)
    assert raw["action"] == "run" and raw["detail"]["reasks"] == 1

    a = verdict(f, gone(), NOW + 5)
    why = "couldn't start the reviewer: a workspace already exists"
    assert a == {
        "action": "run",
        "note": why + " — starting another",
        "detail": {"respawned": why, "reasks": 1},
    }
    # review._run keeps the count (and says why) when it spawns the new one.
    seen: list = []
    titles = iter(["review-2", "review-3"])

    async def spawn(flight, step, prompt, **kw):
        seen.append(kw)
        return next(titles)

    monkeypatch.setattr(D, "spawn_worker", spawn)
    out = asyncio.run(R._run(f, f["steps"][0], a, NOW + 5))
    assert out["detail"] == {
        "respawned": why,
        "reasks": 1,
        "children": ["review-2"],
        "base": "main",
        "reviewed_sha": "",
        "round": 1,
    }
    assert out["note"] == "asking review-2 for a second opinion (%s)" % why
    F.apply(f, out, NOW + 5)
    ss = f["step_states"][sid]
    assert ss["state"] == "running" and ss["rounds"] == 0

    # review-2 can't start either: the second (last) re-ask.
    failures["review-2"] = {"error": "disk full", "ts": NOW + 6}
    entry = R._entry({"title": "review-2", "present": False}, None, None, NOW, NOW)
    b = verdict(f, {"entry": entry, "text": "", "report": False}, NOW + 10)
    assert b["action"] == "run" and b["detail"]["reasks"] == 2
    out = asyncio.run(R._run(f, f["steps"][0], b, NOW + 10))
    F.apply(f, out, NOW + 10)

    # review-3 neither: halt for the person, the real reason.
    failures["review-3"] = {"error": "disk full", "ts": NOW + 11}
    entry = R._entry({"title": "review-3", "present": False}, None, None, NOW, NOW)
    c = verdict(f, {"entry": entry, "text": "", "report": False}, NOW + 15)
    assert c["action"] == "fail" and c["then"] == "halt"
    assert c["reason"] == "couldn't start the reviewer: disk full"
    F.apply(f, c, NOW + 15)
    assert f["state"] == "halted"
    assert f["step_states"][sid]["rounds"] == 0  # no fix round spent
    assert "fix" not in f["step_states"][sid]["detail"]


def test_an_older_single_respawn_counts_as_one_reask():
    assert R.reasks_of({"respawned": "x"}) == 1
    assert R.reasks_of({"respawned": "x", "reasks": 2}) == 2
    assert R.reasks_of({}) == 0


def test_a_failure_older_than_the_stop_is_not_this_reviewers(monkeypatch):
    f = started(monkeypatch, {"review-1": {"error": "old", "ts": NOW - 60}})
    assert D._launch_obs(f["step_states"][f["steps"][0]["id"]]) is None


def test_a_reviewer_gone_before_it_started_without_a_record(monkeypatch):
    f = started(monkeypatch, {})
    # It was seen only while loading, then vanished: nothing recorded why.
    loading = R._entry(
        {"title": "review-1", "present": True, "status": "loading"},
        None,
        None,
        NOW,
        NOW,
    )
    assert not loading.get("started")
    a = verdict(f, gone(loading), NOW + 5)
    assert a["action"] == "run"
    assert a["detail"] == {
        "respawned": "couldn't start the reviewer: " + D.GONE_BEFORE_START,
        "reasks": 1,
    }


def test_a_reviewer_that_started_and_vanished_still_did_not_finish(monkeypatch):
    f = started(monkeypatch, {})
    running = R._entry(
        {"title": "review-1", "present": True, "status": "running"},
        None,
        None,
        NOW,
        NOW,
    )
    assert running["started"] is True
    left = gone(running)
    assert left["entry"]["started"] is True  # sticks once it is gone
    a = verdict(f, left, NOW + 5)
    # It did not finish — still not the agent's: another reviewer is asked.
    assert a["action"] == "run"
    assert a["note"] == "the reviewer did not finish — asking another reviewer"
